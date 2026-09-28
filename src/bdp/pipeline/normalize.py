"""文本与编码归一化。

为什么单独一层
--------------
上游平台编码不规范是电商数据里最普遍、也最容易被低估的问题：
同一个 SKU 的编码可能在 A 平台是全大写、B 平台带前后空格、C 平台用了全角连字符。
如果把这些差异留给下游每一处 JOIN 去处理，代码会迅速失控且必然漏掉某处。

因此统一在这里收敛：**任何跨源关联之前先过一遍归一化**。
"""

from __future__ import annotations

import re
import unicodedata

_WS_RE = re.compile(r"\s+")


def to_halfwidth(s: str) -> str:
    """全角转半角（含全角空格与全角连字符）。"""
    if not s:
        return ""
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def normalize_code(raw: str | None) -> str:
    """编码归一化：去空白 → 全角转半角 → 大写 → 去内部空格。

    用于平台侧编码、条码等"应当唯一"的标识字段。
    """
    if not raw:
        return ""
    s = str(raw)
    s = unicodedata.normalize("NFKC", s)
    s = to_halfwidth(s)
    s = _WS_RE.sub("", s)
    return s.upper()


def normalize_barcode(raw: str | None) -> str:
    """条码归一化：去掉所有非字母数字字符后大写。"""
    if not raw:
        return ""
    s = normalize_code(raw)
    return re.sub(r"[^0-9A-Z]", "", s)


def normalize_text(raw: str | None) -> str:
    """自然语言文本归一化：全角转半角、压缩空白、去首尾。

    注意不做小写化——中文无大小写，英文标题的小写化会损失品牌信息，
    模糊匹配阶段由算法自身处理大小写。
    """
    if not raw:
        return ""
    s = unicodedata.normalize("NFKC", str(raw))
    s = to_halfwidth(s)
    s = _WS_RE.sub(" ", s)
    return s.strip()


def extract_numeric(value) -> float | None:
    """安全地把可能为 None / 字符串的金额转成 float，失败返回 None。

    上游金额字段出现空串、'--'、'NULL' 字符串是常态，不能直接 float()。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = normalize_code(str(value))
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None
