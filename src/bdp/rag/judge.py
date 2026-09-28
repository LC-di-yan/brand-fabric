"""检索充分性判定：决定"这批候选够不够回答"，从而触发改写或多跳。

为什么 RRF 分数不能当判定
--------------------------
RRF/重排分数是**相对排序**信号（候选之间谁更相关），不是**绝对充分性**信号
（这批候选是否足以支撑回答）。判定必须回到查询本身：
查询的词在候选文本里的覆盖度 + 候选数量 + 域匹配度，三者合成一个 0~1 分数。

LLM 判定（pointwise："以下候选能否回答该问题"）是可选增强，接口一致；
LLM 缺位时启发式完整可用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bdp.kb.embedding import tokenize

# 纯语气/敬语填充词：从不承载语义，且几乎不出现在正式语料里，
# 不滤掉会系统性压低口语查询的覆盖度天花板（"不想要了怎么办"类查询）。
_FILLERS = {"吗", "呢", "啊", "呀", "吧", "哦", "嘛", "么", "请问"}


@dataclass
class JudgeResult:
    score: float                  # 0~1，越高越充分
    reasons: list[str] = field(default_factory=list)  # 失配原因：no_hits/low_coverage/few_results/domain_mismatch
    backend: str = "heuristic"


def _content_tokens(text: str) -> set[str]:
    return {t for t in tokenize(text) if t not in _FILLERS}


def _query_coverage(query: str, texts: list[str]) -> float:
    """查询内容词在候选文本并集中的覆盖度（与 retriever 的词权口径一致）。"""
    q_tokens = _content_tokens(query)
    if not q_tokens:
        return 0.0
    union: set[str] = set()
    for t in texts:
        union.update(tokenize(t))
    if not union:
        return 0.0
    # bigram 权重高于 unigram：二元组命中更能代表真实语义覆盖
    hit = sum(2.0 if len(t) >= 2 else 1.0 for t in q_tokens & union)
    total = sum(2.0 if len(t) >= 2 else 1.0 for t in q_tokens)
    return hit / total if total else 0.0


def score_heuristic(
    query: str,
    results: list[dict],
    *,
    kb_type_hint: str | None = None,
) -> JudgeResult:
    """启发式判定：覆盖度为主，候选数与域匹配为修正项。"""
    reasons: list[str] = []
    if not results:
        return JudgeResult(0.0, ["no_hits"], "heuristic")

    top = results[:3]
    lex = sum(r.get("lexical_score", 0.0) for r in top) / len(top)
    cov = _query_coverage(query, [r.get("text", "") for r in top])
    # 词面分（检索器已按查询加权）与并集覆盖度各取所长：前者精确、后者抗同义缺失
    score = 0.5 * lex + 0.5 * cov

    if len(results) < 2:
        reasons.append("few_results")
        score *= 0.7
    if kb_type_hint and all(r.get("kb_type") != kb_type_hint for r in top):
        reasons.append("domain_mismatch")
        score *= 0.6
    if score < 0.5:
        reasons.append("low_coverage")

    return JudgeResult(round(min(score, 1.0), 4), reasons, "heuristic")


def score(
    query: str,
    results: list[dict],
    *,
    kb_type_hint: str | None = None,
    backend: str = "heuristic",
) -> JudgeResult:
    """判定入口。llm 后端不可用时静默回退启发式（判定不能成为故障点）。"""
    if backend == "llm":
        try:
            return _score_llm(query, results, kb_type_hint)
        except Exception:
            pass
    return score_heuristic(query, results, kb_type_hint=kb_type_hint)


def _score_llm(query: str, results: list[dict], kb_type_hint: str | None) -> JudgeResult:
    """LLM pointwise 判定：一段短输出 {relevant: true/false, missing: "..."}。"""
    import json

    from bdp.agents.llm import LLMClient

    llm = LLMClient()
    corpus = "\n\n".join(
        f"[{i}] {r.get('kb_type')}/{r.get('doc_id')}#× {r.get('text', '')[:200]}"
        for i, r in enumerate(results[:5], start=1)
    )
    prompt = (
        "判断以下候选片段是否足以回答问题。只输出 JSON："
        '{"score": 0到1的小数, "missing": "缺少什么信息的简短描述或空串"}\n'
        f"问题：{query}\n\n候选：\n{corpus}"
    )
    message = llm.chat([{"role": "user", "content": prompt}])
    payload = json.loads(str(message.get("content") or "{}"))
    raw = float(payload.get("score", 0.0))
    reasons = ["low_coverage"] if payload.get("missing") else []
    # LLM 分数换算到与启发式同尺度（都落在 0~1、阈值共用）
    return JudgeResult(round(min(max(raw, 0.0), 1.0), 4), reasons, "llm")
