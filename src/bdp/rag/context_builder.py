"""上下文构建：去重 → 重排 → 压缩 → token 预算裁剪。

为什么检索结果不能直接进 prompt
--------------------------------
多跳与多子查询合并后的候选池有三类冗余：完全重复（同 chunk 被多跳命中）、
高度相似（同文档相邻切片）、超长（切片本身千字级）。直接塞 prompt 会
稀释要点并烧 token。这里做三步：按 chunk_id 去重、按"查询相关句"压缩
（保留与查询词重叠度最高的句子）、按预算从高到低裁剪。

压缩是句级抽取式（extractive）而非生成式——无 LLM 依赖，可测试，不引入幻觉。
"""

from __future__ import annotations

import re

from bdp.kb.chunking import split_sentences
from bdp.kb.embedding import tokenize

# 粗略 token 估算：中文 ~1.5 字/token、ASCII ~4 字符/token。
# 只用于预算裁剪，不需要精确 tokenizer。
_CJK = re.compile(r"[\u4e00-\u9fff]")


def estimate_tokens(text: str) -> int:
    """粗略估算文本 token 数（预算用途，非精确计费口径）。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    rest = len(text) - cjk
    return int(cjk / 1.5) + int(rest / 4) + 1


def _compress_sentence(query_tokens: set[str], sentence: str) -> float:
    """单句与查询的词面相关度（句级筛选依据）。"""
    if not query_tokens:
        return 0.0
    s_tokens = set(tokenize(sentence))
    if not s_tokens:
        return 0.0
    return len(query_tokens & s_tokens) / len(query_tokens)


def compress_text(query: str, text: str, *, max_chars: int = 400) -> str:
    """抽取式压缩：按句相关度排序，拼装不超过 max_chars 的前缀。

    句序保持原文顺序（压缩不重排，保证阅读连贯）。
    短文本（本身低于 max_chars）原样返回，零损耗。
    """
    if len(text) <= max_chars:
        return text
    q_tokens = set(tokenize(query))
    sentences = [s.strip() for s in split_sentences(text) if len(s.strip()) >= 6]
    if not sentences:
        return text[:max_chars]
    scored = [(s, _compress_sentence(q_tokens, s)) for s in sentences]
    kept: list[str] = []
    used = 0
    for s, sc in sorted(scored, key=lambda x: -x[1]):
        if sc <= 0:
            continue
        if used + len(s) > max_chars:
            continue
        kept.append(s)
        used += len(s) + 1
        if used >= max_chars * 0.8:
            break
    if not kept:  # 全部零相关时退化为原文前缀（有内容总比空好）
        return text[:max_chars]
    # 恢复原文顺序
    kept_set = set(kept)
    return "\n".join(s for s in sentences if s in kept_set)


def build_context(
    query: str,
    results: list[dict],
    *,
    token_budget: int,
) -> tuple[list[dict], dict]:
    """把候选列表加工为最终上下文块。

    返回 (blocks, stats)：
    blocks: [{doc_id, kb_type, chunk_ix, text, score, source}]（已去重压缩裁剪）
    stats : {input_chunks, deduped, compressed, budget_tokens, used_tokens, dropped}
    """
    stats = {
        "input_chunks": len(results),
        "deduped": 0,
        "compressed": 0,
        "budget_tokens": token_budget,
        "used_tokens": 0,
        "dropped": 0,
    }
    # ---- 去重（按 chunk_id，保留高分版本）----
    by_id: dict[str, dict] = {}
    for r in results:
        cid = r.get("chunk_id")
        if not cid:
            continue
        prev = by_id.get(cid)
        if prev is None or r.get("score", 0) > prev.get("score", 0):
            by_id[cid] = r
    stats["deduped"] = len(results) - len(by_id)

    # ---- 按分数降序处理（保证预算花在最高分上），压缩 + 裁剪 ----
    ordered = sorted(by_id.values(), key=lambda r: -r.get("score", 0.0))
    blocks: list[dict] = []
    for r in ordered:
        raw_text = r.get("text", "")
        budget_left = token_budget - stats["used_tokens"]
        if budget_left <= 200:  # 预算见底，停止加入（保留余量给指令）
            stats["dropped"] += 1
            continue
        compressed = compress_text(query, raw_text, max_chars=min(400, max(budget_left * 2, 120)))
        if compressed != raw_text:
            stats["compressed"] += 1
        cost = estimate_tokens(compressed)
        if cost > budget_left:
            # 单块超预算：再压一轮；仍超则丢弃
            compressed = compress_text(query, raw_text, max_chars=max(budget_left * 2 - 60, 80))
            cost = estimate_tokens(compressed)
            if cost > budget_left:
                stats["dropped"] += 1
                continue
            stats["compressed"] += 1
        stats["used_tokens"] += cost
        blocks.append({
            "doc_id": r.get("doc_id"),
            "kb_type": r.get("kb_type"),
            "chunk_ix": r.get("chunk_ix"),
            "chunk_id": r.get("chunk_id"),
            "text": compressed,
            "score": r.get("score", 0.0),
            "source": r.get("source", "search"),
        })
    return blocks, stats
