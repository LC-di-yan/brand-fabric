"""模拟数据生成入口（门面）。

实现按职责拆分在同目录子模块：
    brands.py        品牌画像 / 数据日历 / 脏数据注入工具
    master_data.py   租户、店铺、商品与跨平台编码映射
    transactions.py  订单与退款
    cs_sessions.py   客服会话
    kb_docs.py       知识文档
    users.py         演示账号

本模块保留 generate_all 编排入口，并 re-export 子模块的全部公开名称，
保证 `from bdp.mock.generator import generate_all` 等既有导入不变。

设计要点（与拆分前一致）：
1. **确定性**：固定随机种子，同一 seed 每次生成完全一致，便于复现与对比实验。
2. **故意注入脏数据**：让数据质量校验有真实的可捕获对象。
3. **业务真实性**：大促峰值、品类差异化退款率、客服机器人占比等按品类建模。

品牌与店铺全部虚构，不含任何真实企业/品牌信息。
"""

from __future__ import annotations

import random

from sqlalchemy.orm import Session

from bdp.config import settings
from bdp.mock.brands import (  # noqa: F401  re-export 兼容旧导入
    BRANDS,
    CS_INTENTS,
    ORDER_STATUS,
    PLATFORMS,
    REFUND_REASONS,
    SPEC_BY_ID,
    WINDOW_END,
    BrandSpec,
    _corrupt_code,
    _dirty_sku_code,
    _iter_days,
    _promo_multiplier,
)
from bdp.mock.cs_sessions import _gen_cs_sessions  # noqa: F401
from bdp.mock.kb_docs import _gen_kb_documents  # noqa: F401
from bdp.mock.master_data import (  # noqa: F401
    _gen_master_data,
    _gen_platform_sku_map,
    _gen_products,
    _sku_specs,
)
from bdp.mock.transactions import _gen_transactions  # noqa: F401
from bdp.mock.users import _gen_users  # noqa: F401


# 入口
# ---------------------------------------------------------------------------


def generate_all(session: Session, *, seed: int | None = None, days: int | None = None) -> dict:
    """生成全量模拟数据，返回统计信息。"""

    rng = random.Random(seed if seed is not None else settings.mock_seed)
    days = days or settings.mock_days

    _, shops = _gen_master_data(session, rng)
    spus, skus = _gen_products(session, rng)
    code_lookup = _gen_platform_sku_map(session, rng, skus, shops)
    n_orders, n_refunds = _gen_transactions(session, rng, skus, shops, days, code_lookup)
    n_sessions = _gen_cs_sessions(session, rng, shops, days)
    n_docs = _gen_kb_documents(session, rng, spus)
    _gen_users(session)

    return {
        "brands": len(BRANDS),
        "shops": len(shops),
        "spus": len(spus),
        "skus": len(skus),
        "orders": n_orders,
        "refunds": n_refunds,
        "cs_sessions": n_sessions,
        "kb_documents": n_docs,
        "days": days,
        "window": f"{_iter_days(days)[0]} ~ {_iter_days(days)[-1]}",
    }
