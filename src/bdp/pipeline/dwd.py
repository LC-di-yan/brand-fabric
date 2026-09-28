"""明细层（dwd）加工：清洗、标准化、质量标记。

原则
----
**不丢弃脏数据，而是打标记。**
上游的问题记录如果直接丢弃，会导致"报表对不上但查不出原因"；
本项目统一保留并打 `is_valid` / `invalid_reason`，让问题可追溯、可统计修复率。

分页读写的实现说明
------------------
加工过程采用「按键分页读取 + 批量写入」而不是流式游标：
在同一个 Session 上一边保持打开的读取游标、一边写入，不同驱动的行为不一致
（psycopg 服务端游标下尤其容易出问题）。按键分页既避免了游标冲突，
又把内存占用控制在单批大小以内，是批处理里更稳的做法。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from bdp.models import DwdCsSession, DwdOrder, DwdRefund, RawCsSession, RawOrder, RawRefund
from bdp.pipeline.mdm import MdmStats, PlatformSkuMatcher, resolve_orders
from bdp.pipeline.normalize import extract_numeric

BATCH_SIZE = 5000

# 金额异常时用于兜底的替代值（保证下游 SUM 不被 None 污染）
AMOUNT_FALLBACK = 0.0


# ---------------------------------------------------------------------------
# 订单明细
# ---------------------------------------------------------------------------


def build_dwd_orders(session: Session, matcher: PlatformSkuMatcher) -> dict:
    session.execute(delete(DwdOrder))

    stats = defaultdict(int)
    mdm = MdmStats()
    last_id = ""
    read = resolved = 0

    while True:
        rows = (
            session.execute(
                select(RawOrder)
                .where(RawOrder.order_line_id > last_id)
                .order_by(RawOrder.order_line_id)
                .limit(BATCH_SIZE)
            )
            .scalars()
            .all()
        )
        if not rows:
            break
        last_id = rows[-1].order_line_id

        # 批量主数据映射：先解析整批，再落库，避免逐行查询
        batch_dicts = [
            {
                "order_line_id": r.order_line_id,
                "tenant_id": r.tenant_id,
                "platform": r.platform,
                "shop_id": r.shop_id,
                "platform_sku_code": r.platform_sku_code,
            }
            for r in rows
        ]
        match_map, batch_stats = resolve_orders(session, matcher, batch_dicts)
        mdm.total += batch_stats.total
        mdm.exact += batch_stats.exact
        mdm.fuzzy += batch_stats.fuzzy
        mdm.unmatched += batch_stats.unmatched
        read += len(rows)
        resolved += 1

        out: list[dict] = []
        for raw in rows:
            match = match_map[raw.order_line_id]
            pay_amount = extract_numeric(raw.pay_amount)
            discount = extract_numeric(raw.discount) or 0.0
            freight = extract_numeric(raw.freight) or 0.0
            qty = extract_numeric(raw.qty) or 0.0

            is_valid, invalid_reason = True, None
            if pay_amount is None:
                is_valid, invalid_reason, pay_amount = False, "pay_amount_null", AMOUNT_FALLBACK
                stats["invalid_amount_null"] += 1
            elif pay_amount <= 0:
                is_valid, invalid_reason, pay_amount = False, "pay_amount_not_positive", AMOUNT_FALLBACK
                stats["invalid_amount_nonpositive"] += 1
            elif qty <= 0 or qty > 100:
                is_valid, invalid_reason = False, "qty_out_of_range"
                stats["invalid_qty"] += 1

            is_cancelled = raw.order_status == "cancelled"
            net_amount = round(pay_amount - freight, 2)

            out.append(
                dict(
                    order_line_id=raw.order_line_id,
                    tenant_id=raw.tenant_id,
                    shop_id=raw.shop_id,
                    platform=raw.platform,
                    order_id=raw.order_id,
                    sku_id=match.sku_id,
                    map_confidence=match.confidence,
                    qty=qty,
                    pay_amount=pay_amount,
                    discount=discount,
                    freight=freight,
                    net_amount=net_amount,
                    order_status=raw.order_status,
                    is_presale=bool(raw.is_presale),
                    is_cancelled=is_cancelled,
                    order_dt=raw.created_at.date(),
                    paid_dt=raw.paid_at.date() if raw.paid_at else None,
                    paid_at=raw.paid_at,
                    is_valid=is_valid,
                    invalid_reason=invalid_reason,
                )
            )

        session.bulk_insert_mappings(DwdOrder, out)
        session.flush()

    stats["raw"] = read
    stats["batches"] = resolved
    stats["map_exact"] = mdm.exact
    stats["map_fuzzy"] = mdm.fuzzy
    stats["map_none"] = mdm.unmatched
    return dict(stats)


# ---------------------------------------------------------------------------
# 退款明细
# ---------------------------------------------------------------------------


def build_dwd_refunds(session: Session) -> dict:
    session.execute(delete(DwdRefund))

    # 预加载订单维度的实付上限与 SKU 归属，避免逐行查询
    order_pay: dict[str, float] = {}
    order_sku: dict[str, str | None] = {}
    for order_id, amount, sku_id in session.execute(
        select(DwdOrder.order_id, func.max(DwdOrder.pay_amount), func.max(DwdOrder.sku_id)).group_by(
            DwdOrder.order_id
        )
    ).all():
        order_pay[order_id] = float(amount or 0.0)
        order_sku[order_id] = sku_id

    stats = defaultdict(int)
    last_id = ""
    read = 0

    while True:
        rows = (
            session.execute(
                select(RawRefund)
                .where(RawRefund.refund_id > last_id)
                .order_by(RawRefund.refund_id)
                .limit(BATCH_SIZE)
            )
            .scalars()
            .all()
        )
        if not rows:
            break
        last_id = rows[-1].refund_id
        read += len(rows)

        out: list[dict] = []
        for raw in rows:
            amount = extract_numeric(raw.refund_amount)
            is_valid, invalid_reason = True, None

            if amount is None:
                is_valid, invalid_reason, amount = False, "refund_amount_null", AMOUNT_FALLBACK
                stats["invalid_null"] += 1
            elif amount <= 0:
                is_valid, invalid_reason, amount = False, "refund_amount_not_positive", AMOUNT_FALLBACK
                stats["invalid_nonpositive"] += 1
            else:
                cap = order_pay.get(raw.order_id)
                if cap is not None and amount > cap + 0.01:
                    is_valid, invalid_reason = False, "refund_exceeds_order_amount"
                    stats["invalid_over_order"] += 1

            out.append(
                dict(
                    refund_id=raw.refund_id,
                    tenant_id=raw.tenant_id,
                    shop_id=raw.shop_id,
                    platform=raw.platform,
                    order_id=raw.order_id,
                    sku_id=order_sku.get(raw.order_id),
                    refund_amount=amount,
                    refund_type=raw.refund_type,
                    reason_code=raw.reason_code,
                    refund_status=raw.refund_status,
                    refund_dt=raw.created_at.date(),
                    is_valid=is_valid,
                    invalid_reason=invalid_reason,
                )
            )

        session.bulk_insert_mappings(DwdRefund, out)
        session.flush()

    stats["raw"] = read
    return dict(stats)


# ---------------------------------------------------------------------------
# 客服会话明细
# ---------------------------------------------------------------------------


def build_dwd_cs(session: Session) -> dict:
    session.execute(delete(DwdCsSession))

    seen: set[str] = set()
    stats = defaultdict(int)
    last_id = ""
    read = 0

    while True:
        rows = (
            session.execute(
                select(RawCsSession)
                .where(RawCsSession.session_id > last_id)
                .order_by(RawCsSession.session_id)
                .limit(BATCH_SIZE)
            )
            .scalars()
            .all()
        )
        if not rows:
            break
        last_id = rows[-1].session_id
        read += len(rows)

        out: list[dict] = []
        for raw in rows:
            if raw.session_id in seen:
                stats["dedup_dropped"] += 1
                continue
            seen.add(raw.session_id)

            fr = extract_numeric(raw.first_response_sec)
            if fr is None or fr < 0:
                fr = 0.0
                stats["first_response_filled"] += 1

            satisfaction = extract_numeric(raw.satisfaction)
            if satisfaction is not None and not (1.0 <= satisfaction <= 5.0):
                satisfaction = None
                stats["satisfaction_dropped"] += 1

            out.append(
                dict(
                    session_id=raw.session_id,
                    tenant_id=raw.tenant_id,
                    shop_id=raw.shop_id,
                    agent_id=raw.agent_id,
                    is_bot=bool(raw.is_bot),
                    first_response_sec=fr,
                    turn_count=int(extract_numeric(raw.turn_count) or 0),
                    resolved=bool(raw.resolved),
                    satisfaction=satisfaction,
                    intent_l1=raw.intent_l1,
                    session_dt=raw.created_at.date(),
                )
            )

        if out:
            session.bulk_insert_mappings(DwdCsSession, out)
            session.flush()

    stats["raw"] = read
    stats["written"] = len(seen)
    return dict(stats)


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


def build_dwd(session: Session) -> dict:
    started = datetime.now()
    matcher = PlatformSkuMatcher.build(session)
    order_stats = build_dwd_orders(session, matcher)
    refund_stats = build_dwd_refunds(session)
    cs_stats = build_dwd_cs(session)

    total_orders = order_stats.get("raw", 0)
    matched = order_stats.get("map_exact", 0) + order_stats.get("map_fuzzy", 0)
    map_rate = round(matched / total_orders, 4) if total_orders else 0.0

    return {
        "elapsed_sec": round((datetime.now() - started).total_seconds(), 2),
        "orders": order_stats,
        "refunds": refund_stats,
        "cs_sessions": cs_stats,
        "mdm": {
            "exact_index_size": matcher.exact_index_size,
            "normalized_conflicts": len(matcher.conflicts),
            "match_rate": map_rate,
        },
    }
