"""文本切片（Chunking）。

为什么切片策略要单独做实验
--------------------------
切片是 RAG 里影响召回质量最大、但最容易被忽略的一环。
同一个知识库，固定长度切片和按语义边界切片，Recall@5 可能差十几个点。
所以本项目提供三种策略并做了对比评测（见 kb/evaluate.py），而不是拍脑袋定一个。

三种策略
--------
- fixed        固定字符长度 + 重叠窗口：实现最简单，容易把一句话切断
- semantic     先按句末标点切句，再贪心合并到长度上限：边界自然，推荐默认
- hierarchical 在 semantic 基础上，给每个切片前缀文档标题：
               适合"标题即答案上下文"的场景（如政策条款），代价是切片变长
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# 句末标点（中英文）
_SENT_END = re.compile(r"(?<=[。！？；!?;])\s*")


@dataclass
class Chunk:
    chunk_ix: int
    text: str

    @property
    def char_len(self) -> int:
        return len(self.text)

    @property
    def content_hash(self) -> str:
        return hashlib.sha1(self.text.encode("utf-8")).hexdigest()[:16]


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_END.split(text) if p and p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def chunk_fixed(text: str, size: int = 200, overlap: int = 40) -> list[Chunk]:
    text = (text or "").strip()
    if not text:
        return []
    if overlap >= size:
        raise ValueError("overlap 必须小于 size")
    chunks, start, ix = [], 0, 0
    while start < len(text):
        piece = text[start : start + size].strip()
        if piece:
            chunks.append(Chunk(ix, piece))
            ix += 1
        if start + size >= len(text):
            break
        start += size - overlap
    return chunks


def chunk_semantic(text: str, max_len: int = 220) -> list[Chunk]:
    """按句子边界贪心合并，尽量不把一句话切断。"""
    sentences = split_sentences(text)
    if not sentences:
        return []

    chunks: list[Chunk] = []
    buf = ""
    ix = 0
    for sent in sentences:
        # 单句超长则硬切
        while len(sent) > max_len:
            head, sent = sent[:max_len], sent[max_len:]
            if buf:
                chunks.append(Chunk(ix, buf.strip()))
                ix += 1
                buf = ""
            chunks.append(Chunk(ix, head.strip()))
            ix += 1
        if len(buf) + len(sent) <= max_len:
            buf += sent
        else:
            if buf:
                chunks.append(Chunk(ix, buf.strip()))
                ix += 1
            buf = sent
    if buf.strip():
        chunks.append(Chunk(ix, buf.strip()))
    return [c for c in chunks if c.text]


def chunk_hierarchical(text: str, title: str, max_len: int = 220) -> list[Chunk]:
    """语义切片 + 标题前缀，给每个切片补充上下文。"""
    prefix = f"【{title}】" if title else ""
    base = chunk_semantic(text, max_len=max(60, max_len - len(prefix)))
    return [
        Chunk(c.chunk_ix, f"{prefix}{c.text}" if prefix else c.text)
        for c in base
    ]


STRATEGIES = {
    "fixed": lambda text, title: chunk_fixed(text),
    "semantic": lambda text, title: chunk_semantic(text),
    "hierarchical": lambda text, title: chunk_hierarchical(text, title),
}


def chunk_text(text: str, title: str = "", strategy: str = "semantic", **kwargs) -> list[Chunk]:
    if strategy not in STRATEGIES:
        raise ValueError(f"未知切片策略：{strategy}，可选 {list(STRATEGIES)}")
    if strategy == "fixed":
        return chunk_fixed(text, **kwargs) if kwargs else chunk_fixed(text)
    return STRATEGIES[strategy](text, title)
