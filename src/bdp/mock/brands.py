"""品牌规格与共享常量：数据日历、脏数据注入工具。

从 generator 拆出：品牌画像（BrandSpec/BRANDS）是所有子生成器的共同输入；
促销日历与编码污染工具保证各子模块注入行为一致（同一 rng 序列下结果可复现）。
"""

from __future__ import annotations

import random

from dataclasses import dataclass, field
from datetime import date, timedelta

# 数据窗口结束日固定，保证可复现
WINDOW_END = date(2026, 9, 25)

PLATFORMS = ["tmall", "jd", "douyin", "pdd", "xhs", "wechat_mini"]

ORDER_STATUS = ["paid", "shipped", "received", "closed", "cancelled"]
REFUND_REASONS = ["size_not_fit", "quality_issue", "no_longer_needed", "wrong_item", "damaged", "late_delivery"]
CS_INTENTS = [
    "logistics", "product_consult", "after_sale", "size_guide", "promotion", "invoice",
    "price_protection", "return_request", "complaint", "other",
]


@dataclass
class BrandSpec:
    tenant_id: str
    name: str
    category_l1: str
    category_l2: str
    shops: list[tuple[str, str]]  # (platform, shop_name)
    spu_count: int
    price_range: tuple[float, float]
    refund_rate: float
    cs_base_per_day: float          # 每店日均会话量基准
    bot_ratio: float                # 机器人接待占比
    first_resp_mean: float          # 平均首响秒数
    promo_affinity: float           # 对大促的敏感度
    product_words: list[str] = field(default_factory=list)


BRANDS: list[BrandSpec] = [
    BrandSpec(
        tenant_id="T001",
        name="NOVA",
        category_l1="服饰",
        category_l2="运动户外",
        shops=[("tmall", "NOVA运动旗舰店"), ("jd", "NOVA京东自营旗舰店"), ("douyin", "NOVA运动服饰专营店")],
        spu_count=40,
        price_range=(159, 899),
        refund_rate=0.15,
        cs_base_per_day=34,
        bot_ratio=0.74,
        first_resp_mean=26,
        promo_affinity=1.6,
        product_words=["速干", "防风", "轻量", "加绒", "透气", "弹力"],
    ),
    BrandSpec(
        tenant_id="T002",
        name="AURORA",
        category_l1="美妆",
        category_l2="护肤",
        shops=[("tmall", "AURORA美妆旗舰店"), ("xhs", "AURORA美妆官方店")],
        spu_count=22,
        price_range=(199, 1280),
        refund_rate=0.06,
        cs_base_per_day=42,
        bot_ratio=0.81,
        first_resp_mean=18,
        promo_affinity=1.3,
        product_words=["修护", "保湿", "美白", "紧致", "控油", "舒缓"],
    ),
    BrandSpec(
        tenant_id="T003",
        name="LUMEN",
        category_l1="3C数码",
        category_l2="智能穿戴",
        shops=[("jd", "LUMEN数码旗舰店"), ("douyin", "LUMEN智能生活店"), ("pdd", "LUMEN官方特卖店")],
        spu_count=18,
        price_range=(299, 3999),
        refund_rate=0.12,
        cs_base_per_day=30,
        bot_ratio=0.69,
        first_resp_mean=31,
        promo_affinity=1.8,
        product_words=["降噪", "长续航", "高清", "智能", "快充", "防水"],
    ),
    BrandSpec(
        tenant_id="T004",
        name="VERDE",
        category_l1="家居",
        category_l2="家纺",
        shops=[("tmall", "VERDE家居旗舰店"), ("wechat_mini", "VERDE家居优选")],
        spu_count=28,
        price_range=(79, 599),
        refund_rate=0.19,
        cs_base_per_day=48,
        bot_ratio=0.62,
        first_resp_mean=38,
        promo_affinity=1.2,
        product_words=["纯棉", "抗菌", "加厚", "亲肤", "四季", "简约"],
    ),
]

SPEC_BY_ID = {b.tenant_id: b for b in BRANDS}


# ---------------------------------------------------------------------------
# 大促与流量日历
# ---------------------------------------------------------------------------


def _promo_multiplier(d: date, affinity: float) -> float:
    """返回当天相对基线的订单倍数。"""
    m = 1.0
    if d == date(2026, 8, 8):
        m *= 1.0 + 3.0 * affinity
    if d == date(2026, 9, 9):
        m *= 1.0 + 2.4 * affinity
    if date(2026, 7, 15) <= d <= date(2026, 7, 20):
        m *= 1.0 + 0.45 * affinity
    if date(2026, 9, 1) <= d <= date(2026, 9, 5):
        m *= 1.0 + 0.30 * affinity
    if d.weekday() >= 5:
        m *= 1.15
    return m


def _iter_days(days: int) -> list[date]:
    return [WINDOW_END - timedelta(days=days - 1 - i) for i in range(days)]


# ---------------------------------------------------------------------------
# 脏数据注入工具
# ---------------------------------------------------------------------------


def _dirty_sku_code(rng: random.Random, code: str, hard: bool) -> str:
    """模拟上游平台编码不规范。

    r < 0.10  大小写不一致      → 归一化可修
    r < 0.18  前后多空格        → 归一化可修
    r < 0.22  全角连字符        → 归一化可修
    r < 0.24  缺失一位数字      → 归一化修不了，必须靠模糊匹配
    """
    r = rng.random()
    if r < 0.10:
        return code.lower()
    if r < 0.18:
        return f" {code} "
    if r < 0.22 and hard:
        return code.replace("-", "－")  # 全角连字符
    if r < 0.24:
        return code.replace("0", "", 1)  # 少一位，用于验证模糊匹配兜底
    return code


def _corrupt_code(rng: random.Random, code: str) -> str:
    """模拟"同一编码在不同批次回传时不一致"，且归一化无法修复的情况。

    用于验证模糊匹配兜底：如果只有归一化，这部分记录会全部映射失败。
    """
    r = rng.random()
    if r < 0.6:
        return code.replace("0", "", 1)          # 少一位
    if r < 0.8:
        idx = max(1, len(code) // 2)
        return code[:idx] + code[idx + 1:]       # 中间缺一个字符
    return code[:-1]                             # 尾部截断
