"""检索效果评测（消融实验）。

为什么要做评测
--------------
"我们做了混合检索、效果更好"是简历上最容易被追问、也最容易答不上来的一句话。
被追问一句"好多少？怎么量的？"就露馅。

本项目把评测做成可复现的实验：
    查询集：从知识库抽样 N 篇文档，用其标题构造查询
    正样本：命中该文档的任意切片即为召回成功（doc 级 ground truth）
    指标：Recall@K 与 MRR@K
    对照组：dense / sparse / hybrid × 是否重排，形成消融矩阵

这样得到的对比数字是**实测**的，而不是拍脑袋写的。
"""

from __future__ import annotations

import random
import time
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from bdp.kb.embedding import build_embedder
from bdp.kb.chunking import split_sentences
from bdp.kb.retriever import search
from bdp.kb.store import build_store
from bdp.models import KbDocument

DEFAULT_SAMPLE = 200


def build_eval_set(session: Session, sample: int = DEFAULT_SAMPLE, seed: int = 42) -> list[dict]:
    """构造评测集。

    查询的构造方式直接决定评测有没有意义。这里刻意避开两个"泄题"做法：
    ① 用标题当查询 —— 层级切片会把标题嵌进每个 chunk，等于把答案写进了候选里；
    ② 用整段正文当查询 —— 与 chunk 高度重合，稀疏通路必然满分。

    实际做法：从正文中抽取一个**不含标题词**的片段作为查询，模拟真实用户
    "描述问题/复述特点"的检索行为。这样稠密通路要靠语义匹配，稀疏通路要靠词面，
    两者的差异才会显现出来。
    """
    docs = session.execute(select(KbDocument)).scalars().all()
    if not docs:
        return []

    rng = random.Random(seed)
    if len(docs) > sample:
        docs = rng.sample(docs, sample)

    eval_set = []
    for doc in docs:
        title = (doc.title or "").strip()
        body = (doc.content or "").replace(title, "")
        for brand in ("NOVA", "AURORA", "LUMEN", "VERDE"):
            body = body.replace(brand, "")
        clauses = [c.strip("　 \n") for c in split_sentences(body) if len(c.strip()) >= 12]
        if not clauses:
            continue
        # 取中后段的句子，避开与标题同源的引入句
        clause = rng.choice(clauses[len(clauses) // 3 :]) if len(clauses) > 2 else rng.choice(clauses)
        query = clause[:28].strip()
        if len(query) < 8:
            continue
        eval_set.append(
            {
                "query": query,
                "tenant_id": doc.tenant_id,
                "kb_type": doc.kb_type,
                "gt_doc_id": doc.doc_id,
            }
        )
    return eval_set


def evaluate_modes(
    session: Session,
    *,
    sample: int = DEFAULT_SAMPLE,
    top_k: int = 5,
    modes: tuple[str, ...] = ("dense", "sparse", "hybrid"),
    rerank_options: tuple[bool, ...] = (False, True),
    store=None,
    embedder=None,
    eval_set: list[dict] | None = None,
) -> dict:
    eval_set = eval_set if eval_set is not None else build_eval_set(session, sample=sample)
    if not eval_set:
        return {"error": "评测集为空，请先执行 kb 入库"}

    store = store or build_store()
    embedder = embedder or build_embedder()

    matrix: list[dict] = []
    for mode in modes:
        for rerank in rerank_options:
            hit_cnt = 0
            rr_sum = 0.0
            latencies: list[float] = []
            for case in eval_set:
                t0 = time.perf_counter()
                res = search(
                    session, query=case["query"], tenant_id=case["tenant_id"],
                    top_k=top_k, store=store, embedder=embedder, mode=mode, rerank=rerank,
                )
                latencies.append((time.perf_counter() - t0) * 1000)

                rank = 0
                for i, r in enumerate(res.get("results", []), start=1):
                    if r.get("doc_id") == case["gt_doc_id"]:
                        rank = i
                        break
                if rank:
                    hit_cnt += 1
                    rr_sum += 1.0 / rank

            n = len(eval_set)
            latencies.sort()
            matrix.append(
                {
                    "mode": mode,
                    "rerank": rerank,
                    "recall_at_k": round(hit_cnt / n, 4),
                    "mrr_at_k": round(rr_sum / n, 4),
                    "queries": n,
                    "p50_latency_ms": round(latencies[n // 2], 2),
                    "p95_latency_ms": round(latencies[min(n - 1, int(n * 0.95))], 2),
                }
            )

    baseline = next((m for m in matrix if m["mode"] == "dense" and not m["rerank"]), None)
    best = max(matrix, key=lambda m: (m["recall_at_k"], m["mrr_at_k"]))
    improvement = None
    if baseline and baseline["recall_at_k"] > 0 and best["recall_at_k"] > baseline["recall_at_k"]:
        improvement = round(
            (best["recall_at_k"] - baseline["recall_at_k"]) / baseline["recall_at_k"] * 100, 1
        )

    return {
        "top_k": top_k,
        "eval_set_size": len(eval_set),
        "embedding_backend": embedder.name,
        "vector_backend": store.backend,
        "matrix": matrix,
        "baseline_dense_recall": baseline["recall_at_k"] if baseline else None,
        "best": {k: best[k] for k in ("mode", "rerank", "recall_at_k", "mrr_at_k", "p95_latency_ms")},
        "relative_improvement_pct": improvement,
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
    }


def run_eval(session: Session, top_k: int = 5, backend: str | None = None, sample: int = DEFAULT_SAMPLE) -> dict:
    store = build_store(backend)
    return evaluate_modes(session, sample=sample, top_k=top_k, store=store)


# ---------------------------------------------------------------------------
# RAG 靶场：复合 / 多跳 / 含糊查询评测（docs/AGENTIC_RAG_PLAN.md §8）
# ---------------------------------------------------------------------------

def build_rag_eval_set(session: Session) -> list[dict]:
    """构造 RAG 管线评测集（复合 + 含糊两类，约 100 条，从真实语料锚定 ground truth）。

    与 build_eval_set（单跳、从语料自动生成）不同，本集合的查询是**人工模式**：
    - compound：一个问题命中两个知识域（复合句），期望两个文档都进候选；
    - colloquial：口语表述（"不想要了"），期望经改写后命中对应政策/FAQ 文档。

    ground truth 用"标题关键词"从库中实际查出（标题格式 f"{品牌} {主题}"），
    语料模板变化时自动跟随；product 域没有稳定标题关键词，回退取该域第一篇。
    """
    docs = session.execute(select(KbDocument)).scalars().all()

    def pick(tenant: str, kb_type: str, title_kw: str | None) -> KbDocument | None:
        cands = [d for d in docs if d.tenant_id == tenant and d.kb_type == kb_type]
        if title_kw:
            hit = [d for d in cands if title_kw in (d.title or "")]
            if hit:
                return hit[0]
        return cands[0] if cands else None

    tenants = sorted({d.tenant_id for d in docs})
    eval_set: list[dict] = []

    # (查询, [(域, 标题关键词), (域, 标题关键词)])——复合句，期望两个文档都命中
    compound_templates = [
        ("退货政策是什么，另外投诉安抚话术怎么说", [("policy", "退货"), ("sop", "投诉")]),
        ("开发票有什么要求，另外发货时效是多久", [("policy", "发票"), ("policy", "发货")]),
        ("运费怎么算，还有色差算质量问题吗", [("cs_faq", "运费"), ("cs_faq", "色差")]),
        ("首响规范是什么，顺便说说面料材质", [("sop", "首响"), ("product", None)]),
        ("价保规则和换货政策分别是什么", [("policy", "价保"), ("policy", "换货")]),
        ("发票可以开吗，以及退款多久到账", [("cs_faq", "发票"), ("cs_faq", "退款")]),
        ("投诉了怎么安抚，另外知识库怎么维护", [("sop", "投诉"), ("sop", "知识库")]),
        ("维修保修政策，还有尺码怎么选", [("policy", "售后"), ("policy", "尺码")]),
        ("会员积分怎么算，以及价保怎么申请", [("policy", "会员"), ("policy", "价保")]),
        ("人工客服怎么转，还有货到付款支持吗", [("cs_faq", "人工"), ("cs_faq", "货到付款")]),
    ]
    # (查询, (域, 标题关键词))——口语化短句，期望经改写后命中正确文档
    colloquial_templates = [
        ("不想要了怎么办", ("policy", "退货")),
        ("尺码不合适能换吗", ("policy", "换货")),
        ("钱什么时候回来", ("cs_faq", "退款")),
        ("多久发货啊", ("cs_faq", "发货")),
        ("开票要什么信息", ("policy", "发票")),
        ("买大了能换吗", ("policy", "换货")),
        ("衣服坏了怎么办", ("policy", "售后")),
        ("降价了能补差价吗", ("policy", "价保")),
        ("怎么转人工", ("cs_faq", "人工")),
        ("积分快过期了怎么办", ("policy", "会员")),
        ("改一下收货地址", ("cs_faq", "收货地址")),
        ("没货了怎么办", ("cs_faq", "断货")),
        ("包装破损了怎么处理", ("cs_faq", "包装")),
        ("可以货到付款吗", ("cs_faq", "货到付款")),
        ("话术不合格会怎样", ("sop", "投诉")),
    ]

    for tenant in tenants:
        for query, specs in compound_templates:
            gt = [d.doc_id for d in (pick(tenant, kt, kw) for kt, kw in specs) if d]
            if len(gt) == len(specs):
                eval_set.append({"query": query, "tenant_id": tenant,
                                 "kind": "compound", "gt_doc_ids": gt})
        for query, (kt, kw) in colloquial_templates:
            d = pick(tenant, kt, kw)
            if d:
                eval_set.append({"query": query, "tenant_id": tenant,
                                 "kind": "colloquial", "gt_doc_ids": [d.doc_id]})
    return eval_set


def evaluate_rag(
    session: Session,
    *,
    strategy: str = "single",
    top_k: int = 5,
    store=None,
    embedder=None,
    eval_set: list[dict] | None = None,
) -> dict:
    """RAG 管线消融评测：single vs agentic 在复合/含糊集上的对比。

    指标：
    - recall_at_k：ground truth 文档被任一候选命中的比例（多跳合并后的候选池口径）
    - avg_hits：平均每个查询命中的 gt 文档数（复合查询的深度信号）
    - p95_latency_ms：管线延迟（agentic 含判定与多跳）
    """
    from bdp.rag.pipeline import answer as rag_answer

    eval_set = eval_set if eval_set is not None else build_rag_eval_set(session)
    if not eval_set:
        return {"error": "RAG 评测集为空，请先执行 kb 入库"}

    store = store or build_store()
    embedder = embedder or build_embedder()

    hit_cases = 0
    total_hits = 0
    latencies: list[float] = []
    degraded_cnt = 0
    for case in eval_set:
        t0 = time.perf_counter()
        rag = rag_answer(
            session, query=case["query"], tenant_id=case["tenant_id"],
            strategy=strategy, top_k=top_k, store=store, embedder=embedder,
        )
        latencies.append((time.perf_counter() - t0) * 1000)
        got = {(b["doc_id"]) for b in rag.blocks}
        hits = sum(1 for g in case["gt_doc_ids"] if g in got)
        total_hits += hits
        if hits:
            hit_cases += 1
        if rag.degraded_reasons:
            degraded_cnt += 1

    n = len(eval_set)
    latencies.sort()
    return {
        "strategy": strategy,
        "eval_set_size": n,
        "recall_at_k": round(hit_cases / n, 4),
        "avg_hits_per_query": round(total_hits / n, 4),
        "degraded_rate": round(degraded_cnt / n, 4),
        "p50_latency_ms": round(latencies[n // 2], 2),
        "p95_latency_ms": round(latencies[min(n - 1, int(n * 0.95))], 2),
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
    }


def compare_chunking(
    session: Session,
    *,
    strategies: tuple[str, ...] = ("fixed", "semantic", "hierarchical"),
    sample: int = 120,
    top_k: int = 5,
) -> dict:
    """切片策略对比：每种策略重新入库到一个独立的本地向量库再评测。

    这是本项目的核心实验之一 —— 切片策略对召回的影响往往大于模型选择。
    """
    from bdp.kb.ingest import ingest_knowledge
    from bdp.kb.store import LocalVectorStore
    from bdp.config import PROJECT_ROOT

    results = []
    eval_set = build_eval_set(session, sample=sample)
    embedder = build_embedder()

    for strategy in strategies:
        store = LocalVectorStore(path=PROJECT_ROOT / "data" / "vector_store_eval" / strategy)
        store.reset()
        t0 = time.perf_counter()
        ingest = ingest_knowledge(session, store=store, strategy=strategy, rebuild=True)
        ingest_sec = round(time.perf_counter() - t0, 2)

        res = evaluate_modes(
            session, top_k=top_k, modes=("hybrid",), rerank_options=(True,),
            store=store, embedder=embedder, eval_set=eval_set,
        )
        results.append(
            {
                "strategy": strategy,
                "chunks": ingest.get("chunks", 0),
                "avg_chunk_len": ingest.get("avg_chunk_len", 0),
                "ingest_sec": ingest_sec,
                "recall_at_k": res["matrix"][0]["recall_at_k"],
                "mrr_at_k": res["matrix"][0]["mrr_at_k"],
                "p95_latency_ms": res["matrix"][0]["p95_latency_ms"],
            }
        )

    best = max(results, key=lambda r: (r["recall_at_k"], r["mrr_at_k"])) if results else None
    return {
        "top_k": top_k,
        "eval_set_size": len(eval_set),
        "results": results,
        "best_strategy": best["strategy"] if best else None,
    }
