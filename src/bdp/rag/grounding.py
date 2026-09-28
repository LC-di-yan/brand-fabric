"""引用核验（Grounding）：把"答案是否可信"从感觉变成数字。

两级核验
--------
1. **存在性**（硬约束）：答案引用的每个 (doc_id, chunk_ix) 必须出现在
   本次检索的候选集里。LLM 编造引用号在这里被拦截——拦截后调用方可
   重试一次生成，仍失败则显式降级。
2. **支撑度**（软信号）：答案与被引切片的词面相似度。无 LLM 时用加权覆盖度，
   有 LLM 时可换向量相似——它不完美（同义改写会低估），因此只用于
   confidence 分档与 degraded 提示，不直接判死。

指标口径（与评测对齐）：
引用精确率 = 被支撑引用数 / 总引用数（support_ratio）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bdp.kb.embedding import tokenize


@dataclass
class GroundingReport:
    total_citations: int
    missing_citations: list[dict] = field(default_factory=list)   # 候选集中不存在的引用
    support_scores: list[float] = field(default_factory=list)     # 每条引用的支撑度 0~1
    support_ratio: float = 0.0                                    # 被支撑引用占比
    mean_support: float = 0.0                                     # 平均支撑度
    confidence: str = "low"                                       # high | medium | low
    ok: bool = False                                              # 引用是否全部可溯源

    def to_dict(self) -> dict:
        return {
            "total_citations": self.total_citations,
            "missing": self.missing_citations,
            "support_ratio": round(self.support_ratio, 4),
            "mean_support": round(self.mean_support, 4),
            "confidence": self.confidence,
            "ok": self.ok,
        }


def _support(answer: str, chunk_text: str) -> float:
    """答案对单个切片的支撑度：答案 token 在切片中的加权覆盖率。"""
    a_tokens = set(tokenize(answer))
    if not a_tokens or not chunk_text:
        return 0.0
    c_tokens = set(tokenize(chunk_text))
    hit = sum(2.0 if len(t) >= 2 else 1.0 for t in a_tokens & c_tokens)
    total = sum(2.0 if len(t) >= 2 else 1.0 for t in a_tokens)
    return hit / total if total else 0.0


# 分档阈值：>=0.45 高（词面口径下强支撑）；>=0.25 中；其余低
_SUPPORT_HIGH = 0.45
_SUPPORT_MEDIUM = 0.25


def check_answer(
    answer: str,
    kb_citations: list[dict],
    candidate_index: set[tuple[str, int]],
    candidate_texts: dict[tuple[str, int], str],
) -> GroundingReport:
    """核验答案引用。

    kb_citations: [{doc_id, kb_type, chunk_ix}]（答案中实际引用的知识来源）
    candidate_index: 检索候选 (doc_id, chunk_ix) 集合
    candidate_texts: 候选全文（支撑度计算用）
    """
    report = GroundingReport(total_citations=len(kb_citations))
    if not kb_citations:
        # 无知识引用的答案（纯指标问答）不存在引用风险，直接判高置信
        report.ok = True
        report.confidence = "high"
        return report

    for c in kb_citations:
        key = (str(c.get("doc_id")), int(c.get("chunk_ix", 0) or 0))
        if key not in candidate_index:
            report.missing_citations.append(c)
            continue
        report.support_scores.append(_support(answer, candidate_texts.get(key, "")))

    total = len(kb_citations)
    supported = len(report.support_scores)
    report.support_ratio = supported / total if total else 0.0
    report.mean_support = (
        sum(report.support_scores) / supported if supported else 0.0
    )
    report.ok = not report.missing_citations

    if report.missing_citations:
        report.confidence = "low"          # 存在编造引用：无论如何不给高置信
    elif report.mean_support >= _SUPPORT_HIGH:
        report.confidence = "high"
    elif report.mean_support >= _SUPPORT_MEDIUM:
        report.confidence = "medium"
    else:
        report.confidence = "low"
    return report
