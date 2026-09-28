"""检索服务：双路召回 → RRF 融合 → 重排。

为什么必须做混合检索
--------------------
纯稠密向量在"专有名词、型号、货号、尺码"这类查询上召回很差 ——
语义相近但字面不同的文本反而得分高。而电商客服问句里恰恰大量出现这类词
（"AJ1 中帮 42 码"、"发票怎么开"）。
所以采用 dense + sparse 双路召回，用 RRF 做无序融合（不需要归一化两路分数量纲），
再用重排模型/词面重叠加权，把最终顺序调准。

RRF 公式：score(d) = Σ_i 1 / (k + rank_i(d))，k 默认 60
"""

from __future__ import annotations

from rapidfuzz import fuzz

from bdp.kb.embedding import SparseEncoder, default_embedder, tokenize
from bdp.kb.store import DEFAULT_RRF_K, SearchHit, VectorStore, default_store

ALL_KB_TYPES = ("cs_faq", "product", "policy", "sop")

# 各通路召回数量：候选池要明显大于最终输出，否则融合与重排没有发挥空间
RECALL_MULTIPLIER = 6
MIN_RECALL = 20
RERANK_POOL = 30

# 重排权重：RRF 反映双路共识，词面覆盖度抑制"语义漂移"
RERANK_W_RRF = 0.55
RERANK_W_LEXICAL = 0.45

def rrf_fuse(rankings: list[list[str]], k: int = DEFAULT_RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion：只依赖排名、不依赖分数，天然免疫两路分数量纲差异。"""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


# 词权：中文二元组比单字更具体，拉丁词通常是型号/货号，都很重要
_W_LATIN = 1.6
_W_CJK_BIGRAM = 2.2
_W_CJK_UNIGRAM = 1.0


def _token_weight(tok: str) -> float:
    if tok.isascii():
        return _W_LATIN
    return _W_CJK_BIGRAM if len(tok) >= 2 else _W_CJK_UNIGRAM


def _lexical_score(query: str, text: str) -> float:
    """查询词在切片中的加权覆盖率。

    为什么不用 fuzz.partial_ratio 这类模糊比率
    ------------------------------------------
    模糊比率只看"最相似的子串"，对短查询噪声极大：问"退货政策"时，
    只要切片里出现"退货"两个字就能拿到不低的分数，哪怕整片讲的是发票红冲。
    改用"查询词覆盖度 + 词权"后，包含"退货政策"完整二元组的切片会明显胜出。

    生产环境应替换为 bge-reranker 等交叉编码器；这里的选择是为了零依赖可复现。
    """
    if not query or not text:
        return 0.0
    q_tokens = set(tokenize(query))
    if not q_tokens:
        return 0.0
    d_tokens = set(tokenize(text))
    denom = sum(_token_weight(t) for t in q_tokens)
    if denom <= 0:
        return 0.0
    num = sum(_token_weight(t) for t in (q_tokens & d_tokens))
    return round(num / denom, 4)


def _lexical_score_v1_fuzzy(query: str, text: str) -> float:
    """保留原模糊比率实现，用于对比实验（不作为默认）。"""
    if not text:
        return 0.0
    partial = fuzz.partial_ratio(query, text) / 100.0
    token_set = fuzz.token_set_ratio(query, text) / 100.0
    return round(0.45 * partial + 0.55 * token_set, 4)


def search(
    session,  # noqa: ARG001  预留：真实场景需要回表校验文档状态与权限
    *,
    query: str,
    tenant_id: str | None,
    kb_type: str | None = None,
    top_k: int = 5,
    store: VectorStore | None = None,
    embedder=None,
    mode: str = "hybrid",
    rerank: bool = True,
) -> dict:
    """执行检索。

    tenant_id 为 None 时（仅 admin 平台级）不限定租户并给出告警标记；
    正常情况下 tenant_id 由服务层从 token 注入，不由调用方决定。
    """
    if not query or not query.strip():
        return {"query": query, "results": [], "mode": mode, "error": "查询为空"}

    # 平台级身份（admin）不允许不带租户地检索知识库：知识内容与品牌强绑定，
    # 无租户检索等于跨品牌泄漏。这里显式拒绝而不是静默放行。
    if not tenant_id:
        return {
            "query": query, "results": [], "mode": mode,
            "warning": "未指定租户，为避免跨品牌泄漏已拒绝执行",
        }

    store = store or default_store()
    embedder = embedder or default_embedder()

    kb_types = (kb_type,) if kb_type else ALL_KB_TYPES
    recall_limit = max(MIN_RECALL, top_k * RECALL_MULTIPLIER)

    query_dense = embedder.encode([query])[0]
    query_sparse = SparseEncoder.encode(query)

    candidates: dict[str, SearchHit] = {}
    per_type_hits: dict[str, int] = {}
    # 关键：先把各知识域的召回结果**按分数汇总**，再全局排一次名。
    # 如果按知识域顺序直接拼接排名列表，排在后面的知识域（如 policy）会被
    # 系统性低估 —— 即使它在该域内排第 1，拼到全局也可能掉到 20 名开外，
    # 进不了候选池。这是 RRF 实现里非常隐蔽的一个坑。
    dense_scores: dict[str, float] = {}
    sparse_scores: dict[str, float] = {}

    for kt in kb_types:
        if mode in ("dense", "hybrid"):
            for cid, score in store.dense_search(kt, query_dense, tenant_id, recall_limit):
                dense_scores[cid] = max(dense_scores.get(cid, float("-inf")), score)
                candidates.setdefault(cid, SearchHit(cid, tenant_id, kt, "", 0, "", score, "dense"))
        if mode in ("sparse", "hybrid"):
            for cid, score in store.sparse_search(kt, query_sparse, tenant_id, recall_limit):
                sparse_scores[cid] = max(sparse_scores.get(cid, float("-inf")), score)
                candidates.setdefault(cid, SearchHit(cid, tenant_id, kt, "", 0, "", score, "sparse"))
        per_type_hits[kt] = len(candidates)

    dense_ranking = [cid for cid, _ in sorted(dense_scores.items(), key=lambda kv: -kv[1])]
    sparse_ranking = [cid for cid, _ in sorted(sparse_scores.items(), key=lambda kv: -kv[1])]

    if not candidates:
        return {"query": query, "results": [], "mode": mode, "candidates": 0}

    # ---- 融合 ----
    if mode == "dense":
        fused = {cid: hit.score for cid, hit in candidates.items()}
    elif mode == "sparse":
        fused = {cid: hit.score for cid, hit in candidates.items()}
    else:
        rankings = [r for r in (dense_ranking, sparse_ranking) if r]
        fused = rrf_fuse(rankings)

    # ---- 取候选池 ----
    pool_ids = [cid for cid, _ in sorted(fused.items(), key=lambda kv: -kv[1])[:RERANK_POOL]]

    # ---- 回表取文本 ----
    fetched = store.fetch(pool_ids)
    for cid in pool_ids:
        if cid in fetched and cid in candidates:
            hit = fetched[cid]
            candidates[cid].text = hit.text
            candidates[cid].doc_id = hit.doc_id
            candidates[cid].chunk_ix = hit.chunk_ix
            candidates[cid].kb_type = hit.kb_type

    # ---- 重排 ----
    max_rrf = max(fused.values()) if fused else 1.0
    scored: list[tuple[str, float, float]] = []
    for cid in pool_ids:
        hit = candidates[cid]
        rrf_norm = fused.get(cid, 0.0) / max_rrf if max_rrf > 0 else 0.0
        if rerank:
            lex = _lexical_score(query, hit.text)
            final = RERANK_W_RRF * rrf_norm + RERANK_W_LEXICAL * lex
        else:
            lex = None
            final = rrf_norm
        scored.append((cid, final, lex if lex is not None else 0.0))

    scored.sort(key=lambda x: -x[1])

    results = []
    for cid, final, lex in scored[:top_k]:
        hit = candidates[cid]
        results.append(
            {
                "chunk_id": cid,
                "doc_id": hit.doc_id,
                "kb_type": hit.kb_type,
                "chunk_ix": hit.chunk_ix,
                "text": hit.text,
                "score": round(final, 6),
                "rrf_score": round(fused.get(cid, 0.0), 6),
                "lexical_score": round(lex, 4),
                "source": hit.source,
            }
        )

    return {
        "query": query,
        "tenant_id": tenant_id,
        "kb_type": kb_type or "all",
        "mode": mode,
        "rerank": rerank,
        "candidates": len(candidates),
        "per_kb_type": per_type_hits,
        "results": results,
    }
