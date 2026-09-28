"""有界多跳扩展：文档内边 + 实体种子边。

为什么只做两条边
----------------
多跳的价值是把"第一跳命中的线索"变成"第二跳的查询"，但它天然带噪声风险：
每多一跳，候选池就多一分污染。本模块只开放两条**有明确语义的边**：

1. 文档内边：命中切片的同文档相邻切片（chunk_id 可构造直取，零检索成本）——
   补齐切片切断的上下文；
2. 实体种子边：从命中文本抽取结构化编号（POLICY-12 / SOP-9 / FAQ-3）与品牌名，
   作为第二跳查询跨知识域检索——覆盖"政策里引用 SOP 编号"这类真实关联。

两条边都受 max_hops 预算硬顶，且**每一跳都强制携带同一租户**走
retriever.search（其内部对无租户调用直接拒绝，见 kb/retriever.py）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bdp.kb.retriever import search as kb_search

# 语料中的结构化编号：source_id 规则见 mock/kb_docs.py（POLICY-*/SOP-*/FAQ-*）
_ENTITY_ID = re.compile(r"\b((?:POLICY|SOP|FAQ)-\d+)\b")
# 品牌名（同语料 BRANDS）；以"品牌名 + 空格 + 主题"为标题，品牌名可当检索种子
_BRAND = re.compile(r"\b(NOVA|AURORA|LUMEN|VERDE)\b")


@dataclass
class HopSeed:
    """第二跳种子：一条查询 + 域提示 + 来源引用（trace 可回放）。"""

    query: str
    kb_type: str | None
    from_citation: dict
    edge: str  # "neighbor" | "entity"


def _neighbor_chunk_ids(top_results: list[dict], per_doc: int = 1) -> list[str]:
    """构造同文档相邻切片的 chunk_id（格式 {doc_id}#{ix:03d}，见 kb/ingest.py）。"""
    ids: list[str] = []
    seen_docs: set[str] = set()
    for r in top_results:
        doc_id = r.get("doc_id") or ""
        if not doc_id or doc_id in seen_docs:
            continue
        seen_docs.add(doc_id)
        try:
            ix = int(r.get("chunk_ix", 0))
        except (TypeError, ValueError):
            continue
        for delta in (-1, 1):
            neighbor = f"{doc_id}#{max(ix + delta, 0):03d}"
            if neighbor != f"{doc_id}#{ix:03d}":
                ids.append(neighbor)
        if len(seen_docs) >= per_doc * 4:
            break
    return ids


def _entity_seeds(top_results: list[dict], exclude_query: str) -> list[HopSeed]:
    """从命中文本抽取实体编号/品牌名作为第二跳查询种子。"""
    seeds: list[HopSeed] = []
    seen_terms: set[str] = set()
    for r in top_results:
        text = r.get("text", "")
        citation = {"doc_id": r.get("doc_id"), "kb_type": r.get("kb_type"),
                    "chunk_ix": r.get("chunk_ix")}
        for term in [*_ENTITY_ID.findall(text), *_BRAND.findall(text)]:
            if term in seen_terms or term in exclude_query:
                continue
            seen_terms.add(term)
            seeds.append(HopSeed(
                query=term,
                kb_type=None,  # 实体跨域：不限定 kb_type，让检索器自己找
                from_citation=citation,
                edge="entity",
            ))
        if len(seeds) >= 3:
            break
    return seeds


def expand(
    session,
    top_results: list[dict],
    *,
    tenant_id: str,
    max_hops: int,
    store=None,
    embedder=None,
    top_k: int = 3,
) -> tuple[list[dict], list[dict]]:
    """执行多跳扩展，返回 (新增候选, trace)。

    边 1（文档内）用 store.fetch 直取相邻切片（不产生检索查询）；
    边 2（实体种子）用检索查询实现，检索成本受 max_hops 与种子数上限约束。
    tenant_id 透传给每一次检索——多跳不豁免租户隔离。
    """
    trace: list[dict] = []
    if max_hops <= 0 or not top_results:
        return [], trace

    new_hits: list[dict] = []

    # ---- 边 1：同文档相邻切片 ----
    from bdp.kb.store import default_store as _default_store

    store = store or _default_store()
    neighbor_ids = _neighbor_chunk_ids(top_results)
    if neighbor_ids:
        fetched = store.fetch(neighbor_ids)
        for cid, hit in fetched.items():
            if any(h.get("chunk_id") == cid for h in top_results):
                continue
            new_hits.append({
                "chunk_id": cid, "doc_id": hit.doc_id, "kb_type": hit.kb_type,
                "chunk_ix": hit.chunk_ix, "text": hit.text,
                "score": 0.0, "rrf_score": 0.0, "lexical_score": 0.0,
                "source": "neighbor_hop",
            })
        trace.append({"step": "hop_neighbor", "requested": len(neighbor_ids),
                      "fetched": len(fetched)})

    # ---- 边 2：实体种子检索（消耗 1 跳预算） ----
    if max_hops >= 2:
        existing_ids = {h.get("chunk_id") for h in top_results}
        for seed in _entity_seeds(top_results, exclude_query=""):
            res = kb_search(
                session, query=seed.query, tenant_id=tenant_id,
                kb_type=seed.kb_type, top_k=top_k, store=store, embedder=embedder,
            )
            added = 0
            for r in res.get("results", []):
                if r["chunk_id"] in existing_ids:
                    continue
                r = dict(r, source="entity_hop")
                new_hits.append(r)
                existing_ids.add(r["chunk_id"])
                added += 1
            trace.append({
                "step": "hop_entity", "seed": seed.query,
                "from": seed.from_citation, "added": added,
            })

    return new_hits, trace
