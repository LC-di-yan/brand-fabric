"""Agentic RAG 管线编排：single 直通 / agentic 有界循环。

等价性承诺（P0 出口条件）
--------------------------
strategy="single" 时内部**直接调用 kb/retriever.search 并原样返回**，
与升级前行为逐字段一致——这使"上线新代码、不开新行为"成为可能：
默认配置下全系统行为不变，agentic 是显式选择。

agentic 主循环（预算制）：
    plan（分类/拆分/消解）
      → [检索所有子查询 → judge → 不充分则改写重来] × max_rewrites
      → multi_hop（max_hops 预算）
      → context_builder（去重/压缩/裁剪）
      → grounding 存在性预检（在管线内做引用完备性；答案级支撑度由调用方在生成后核验）

全程落 trace（每步可回放）；检索原语始终是 kb/retriever.search——
其内部对无租户调用直接拒绝，管线的每一跳因此天然强制租户隔离。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bdp.config import settings
from bdp.kb.retriever import search as kb_search
from bdp.kb.store import default_store


@dataclass
class RagResult:
    """管线统一产物：single 与 agentic 共用同一结构。"""

    query: str
    tenant_id: str | None
    strategy: str
    blocks: list[dict] = field(default_factory=list)      # 最终上下文块（已压缩）
    citations: list[dict] = field(default_factory=list)   # 知识引用（doc_id/kb_type/chunk_ix）
    trace: list[dict] = field(default_factory=list)       # 管线条步（可回放）
    metric_codes: list[str] = field(default_factory=list) # 规划识别出的指标意图（供调用方走指标工具）
    retrieval_confidence: float = 0.0                     # 检索充分性（0~1）
    degraded_reasons: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _collect_citations(results: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for r in results:
        key = (r.get("doc_id"), int(r.get("chunk_ix", 0) or 0))
        if key in seen:
            continue
        seen.add(key)
        out.append({"doc_id": r.get("doc_id"), "kb_type": r.get("kb_type"),
                    "chunk_ix": r.get("chunk_ix")})
    return out


def _search_single(session, *, query, tenant_id, kb_type, top_k, store, embedder) -> dict:
    return kb_search(
        session, query=query, tenant_id=tenant_id, kb_type=kb_type,
        top_k=top_k, store=store, embedder=embedder,
    )


def answer(
    session,
    *,
    query: str,
    tenant_id: str | None,
    strategy: str | None = None,
    history: list[dict] | None = None,
    top_k: int | None = None,
    kb_type: str | None = None,
    store=None,
    embedder=None,
) -> RagResult:
    """管线入口。

    - strategy=None 读配置；"single" 直通等价；"agentic" 走完整管线
    - tenant_id 语义与 kb/retriever.search 一致（None 会被检索器拒绝）
    - history 是会话历史 [{question, answer}]，用于指代消解（agentic 才使用）
    - kb_type 是调用方的显式域限定（如 InsightAgent 的 LLM 指定 kb_type）；
      agentic 路径下作为子查询缺省域提示（子查询自身的词表提示优先）
    """
    strategy = strategy or settings.rag_strategy
    top_k = top_k or settings.rag_top_k
    store = store or default_store()

    if strategy != "agentic":
        # ---- single：直通等价路径。除参数透传外零逻辑，保证与升级前逐字段一致 ----
        res = _search_single(session, query=query, tenant_id=tenant_id,
                             kb_type=kb_type, top_k=top_k, store=store, embedder=embedder)
        results = res.get("results", [])
        return RagResult(
            query=query, tenant_id=tenant_id, strategy="single",
            blocks=[dict(r, source="search") for r in results],
            citations=_collect_citations(results),
            trace=[{"step": "single_search", "query": query, "hits": len(results)}],
            metric_codes=[],
            retrieval_confidence=1.0 if results else 0.0,
            degraded_reasons=[r for r in (res.get("warning"),) if r],
            stats={"candidates": res.get("candidates", 0)},
        )

    # ---------------- agentic ----------------
    from bdp.rag.context_builder import build_context
    from bdp.rag.judge import score as judge_score
    from bdp.rag.multi_hop import expand as multi_hop_expand
    from bdp.rag.query_planner import plan as planner_plan, rewrite as planner_rewrite

    trace: list[dict] = []
    warnings: list[str] = []
    plan = planner_plan(query, history=history)
    trace.extend(plan.trace)
    # 指标意图不属于知识检索：记录给调用方（InsightAgent 会走 query_metric 工具）
    if plan.metric_codes:
        trace.append({"step": "metric_intent", "codes": plan.metric_codes})
    knowledge_subs = [s for s in plan.subqueries if s.intent in ("knowledge", "hybrid")]
    if not knowledge_subs:
        knowledge_subs = plan.subqueries[:1]  # 兜底：至少检索一次，保证不劣化
    if kb_type:
        # 调用方显式域限定 > 词表提示（调用方知道自己在问哪个域）
        knowledge_subs = [
            type(s)(text=s.text, intent=s.intent, kb_type=s.kb_type or kb_type,
                    metric_codes=s.metric_codes)
            for s in knowledge_subs
        ]

    budget = 1 + max(0, settings.rag_max_rewrites)
    best: list[dict] = []
    best_score = -1.0
    current: list = knowledge_subs

    for attempt in range(budget):
        attempt_hits: list[dict] = []
        attempt_scores: list[float] = []
        failed_subs: list = []
        for sub in current:
            res = _search_single(
                session, query=sub.text, tenant_id=tenant_id,
                kb_type=sub.kb_type, top_k=top_k, store=store, embedder=embedder,
            )
            if res.get("warning"):
                warnings.append(res["warning"])
                trace.append({"step": "rejected", "query": sub.text,
                              "reason": res["warning"]})
                continue
            results = res.get("results", [])
            verdict = judge_score(
                sub.text, results, kb_type_hint=sub.kb_type,
                backend=settings.rag_judge_backend,
            )
            # 域解锁重试：词表域提示是先验不是铁律——提示域检索不充分时，
            # 解锁全域重查一次（"多久发货啊"提示 policy，真答案可能在 cs_faq）。
            if sub.kb_type and (results == [] or verdict.score < settings.rag_judge_threshold):
                res2 = _search_single(
                    session, query=sub.text, tenant_id=tenant_id,
                    kb_type=None, top_k=top_k, store=store, embedder=embedder,
                )
                v2 = judge_score(sub.text, res2.get("results", []),
                                 kb_type_hint=None, backend=settings.rag_judge_backend)
                trace.append({
                    "step": "domain_unlock", "query": sub.text,
                    "hinted": sub.kb_type, "hinted_score": verdict.score,
                    "open_score": v2.score,
                })
                if v2.score > verdict.score:
                    res, verdict = res2, v2
                    results = res2.get("results", [])

            attempt_hits.extend(dict(r, source="search") for r in results)
            attempt_scores.append(verdict.score)
            trace.append({
                "step": "retrieve", "attempt": attempt, "query": sub.text,
                "kb_type": sub.kb_type, "hits": len(results),
                "judge": verdict.score, "misses": verdict.reasons,
                "judge_backend": verdict.backend,
            })
            if verdict.score < settings.rag_judge_threshold:
                failed_subs.append((sub, verdict.reasons))

        score_avg = sum(attempt_scores) / len(attempt_scores) if attempt_scores else 0.0
        if attempt_hits and score_avg > best_score:
            best, best_score = attempt_hits, score_avg

        if not failed_subs or attempt == budget - 1:
            break

        # 改写只针对未达标子查询：达标子查询的命中保留参与合并（修复复合题丢文档）
        rewritten_subs: list = []
        for sub, misses in failed_subs:
            rewritten = planner_rewrite(sub.text, misses or ["low_coverage"], kb_type=sub.kb_type)
            trace.append({"step": "rewrite", "attempt": attempt,
                          "from": sub.text, "to": rewritten})
            if rewritten != sub.text:
                rewritten_subs.append(type(sub)(text=rewritten, intent=sub.intent,
                                                kb_type=sub.kb_type,
                                                metric_codes=sub.metric_codes))
        if not rewritten_subs:
            break
        current = rewritten_subs

    # ---- 多跳扩展（基于当前最优候选）----
    hop_hits, hop_trace = multi_hop_expand(
        session, sorted(best, key=lambda r: -r.get("score", 0.0))[:3],
        tenant_id=tenant_id, max_hops=settings.rag_max_hops,
        store=store, embedder=embedder, top_k=top_k,
    )
    trace.extend(hop_trace)

    # ---- 上下文构建（去重/压缩/预算）----
    merged = best + hop_hits
    blocks, ctx_stats = build_context(
        query, merged, token_budget=settings.rag_compress_token_budget,
    )
    trace.append({"step": "context", **ctx_stats})

    degraded: list[str] = list(dict.fromkeys(warnings))  # 检索器拒绝原因透传（去重保序）
    if not blocks:
        degraded.append("no_useful_hits")
    elif best_score < settings.rag_judge_threshold:
        degraded.append("low_retrieval_confidence")

    return RagResult(
        query=query, tenant_id=tenant_id, strategy="agentic",
        blocks=blocks,
        citations=_collect_citations(blocks),
        trace=trace,
        metric_codes=plan.metric_codes,
        retrieval_confidence=round(min(max(best_score, 0.0), 1.0), 4),
        degraded_reasons=degraded,
        stats={
            "attempts": len([t for t in trace if t.get("step") == "retrieve"]),
            "hops": len([t for t in trace if t.get("step", "").startswith("hop_")]),
            **ctx_stats,
        },
    )
