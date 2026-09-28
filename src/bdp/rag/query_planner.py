"""查询规划器：分类、拆分、改写、指代消解。

为什么规则先行
--------------
LLM 规划质量高但有成本、延迟与可用性代价。本模块把"理解查询"拆成
规则可覆盖的部分（词表分类/连接词拆分/同义扩展/槽位继承）与
LLM 可增强的部分（复杂指代/自由改写）——LLM 缺位时规则版完整可用，
LLM 在位时结果更聪明，但**接口与数据结构完全一致**。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bdp.rag.lexicon import (
    CONNECTORS,
    TEMPORAL_TERMS,
    lookup_kb_domains,
    lookup_metric_terms,
    lookup_synonyms,
)

Intent = str  # "metric" | "knowledge" | "hybrid"


@dataclass
class SubQuery:
    """拆分后的子查询：带意图与知识域提示，供检索与调用方分别处理。"""

    text: str
    intent: Intent
    kb_type: str | None = None      # 知识域提示（None = 全域）
    metric_codes: list[str] = field(default_factory=list)


@dataclass
class QueryPlan:
    """一次规划的全部产出：原始查询、分类、子查询队列、改写轨迹。"""

    original: str
    intent: Intent
    subqueries: list[SubQuery]
    metric_codes: list[str]         # 全局命中的指标代码（供调用方走指标工具）
    trace: list[dict] = field(default_factory=list)


def classify(text: str) -> Intent:
    """规则分类：命中指标词表 → metric；命中知识词表 → knowledge；都命中 → hybrid。

    都不命中时保守归为 knowledge（原文直检索，等价现状，保证不劣化）。
    """
    has_metric = bool(lookup_metric_terms(text))
    has_kb = bool(lookup_kb_domains(text))
    if has_metric and has_kb:
        return "hybrid"
    if has_metric:
        return "metric"
    if has_kb:
        return "knowledge"
    return "knowledge"


def _split_by_connectors(text: str) -> list[str]:
    """按连接词切分，保留语义完整的片段（去掉空片段与纯标点）。"""
    segments = [text]
    for conn in CONNECTORS:
        nxt: list[str] = []
        for seg in segments:
            nxt.extend(p.strip() for p in seg.split(conn))
        segments = nxt
    return [s for s in segments if len(s) >= 2]


def _subquery_for(seg: str) -> SubQuery:
    intent = classify(seg)
    domains = lookup_kb_domains(seg)
    # 一个片段可能命中多个域：主域取第一个，其余留作改写提示
    kb_type = domains[0] if domains else None
    return SubQuery(
        text=seg,
        intent=intent,
        kb_type=kb_type,
        metric_codes=lookup_metric_terms(seg),
    )


def decompose(text: str) -> list[SubQuery]:
    """拆分子查询：按连接词切分后逐段分类；切不动就整句作为一个子查询。"""
    segments = _split_by_connectors(text)
    subs = [_subquery_for(s) for s in segments]
    if len(subs) <= 1:
        return [_subquery_for(text)]
    # 丢弃退化片段（切分产生的孤字、纯语气词）
    return [s for s in subs if len(s.text) >= 2]


def resolve_coreference(text: str, history: list[dict]) -> tuple[str, bool]:
    """指代消解（规则版）：对"那上个月呢"类短追问做槽位继承。

    规则：当前查询很短（≤12 字）、含时间词或以指代词开头、且历史上一轮
    存在非空查询时，把上一轮查询里的**指标/知识核心词**嫁接到本轮的时间词上。
    返回 (消解后查询, 是否发生消解)。

    LLM 版消解（自由改写）是可选增强，接口与本函数一致。
    """
    stripped = text.strip()
    if not history or len(stripped) > 12:
        return text, False
    has_temporal = any(t in stripped for t in TEMPORAL_TERMS)
    starts_deictic = stripped.startswith(("那", "它", "这个", "该", "再", "以及"))
    if not (has_temporal or starts_deictic):
        return text, False

    prev = history[-1].get("question", "")
    prev_codes = lookup_metric_terms(prev)
    prev_domains = lookup_kb_domains(prev)
    if prev_codes:
        # 指标槽位继承：保留原时间词 + 上一轮的指标词表首个 surface 对不上没关系，
        # 直接用指标代码对应的中性表述"该指标"会让检索退化，因此用上一轮原查询
        # 中的指标片段（prev 里去掉时间词后的剩余核心段）。
        core = prev
        for t in TEMPORAL_TERMS:
            core = core.replace(t, "")
        core = core.strip(" 的呢吗？?，,")
        if core and core != stripped:
            return f"{stripped} {core}", True
    if prev_domains:
        return f"{stripped} {prev_domains[0]}", True
    return text, False


def rewrite(text: str, misses: list[str], *, kb_type: str | None = None) -> str:
    """规则改写：根据判定器给出的失配原因做有方向的扩展。

    - no_hits / low_coverage：追加同义词表扩展（口语 → 标准语料词）
    - domain_mismatch：追加知识域关键词（把检索拉回正确域）
    - 追加后与原文相同说明无词可扩，返回原文（调用方据此停止循环）
    """
    parts: list[str] = [text]
    synonyms = lookup_synonyms(text)
    if synonyms:
        parts.extend(synonyms)
    if "domain_mismatch" in misses and kb_type:
        domain_keyword = {
            "policy": "政策",
            "cs_faq": "常见问题",
            "sop": "话术 规范",
            "product": "商品 材质",
        }.get(kb_type)
        if domain_keyword and domain_keyword not in text:
            parts.append(domain_keyword)
    # 去重并保持顺序：与原文完全相同视为"改无可改"
    rewritten = " ".join(dict.fromkeys(" ".join(parts).split()))
    if rewritten == text:
        return text
    return rewritten


def plan(text: str, history: list[dict] | None = None) -> QueryPlan:
    """规划入口：消解 → 分类 → 拆分。trace 记录每一步可回放。"""
    history = history or []
    resolved, resolved_flag = resolve_coreference(text, history)
    intent = classify(resolved)
    subs = decompose(resolved)
    metric_codes = sorted({c for s in subs for c in s.metric_codes})
    trace = [
        {"step": "coreference", "resolved": resolved_flag, "query": resolved},
        {"step": "classify", "intent": intent},
        {"step": "decompose",
         "subqueries": [{"text": s.text, "intent": s.intent, "kb_type": s.kb_type}
                        for s in subs]},
    ]
    return QueryPlan(
        original=text, intent=intent, subqueries=subs,
        metric_codes=metric_codes, trace=trace,
    )
