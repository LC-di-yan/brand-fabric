"""汇总层（dws）加工：按"事件发生日"轻度汇总。

口径说明（这是本项目最需要讲清楚的一点）
------------------------------------------
同一张 dws 表里同时承载三种口径的数据，但**按各自的事件日归集**：
    order_gmv     —— 按**下单日**（order_dt）归集
    paid_gmv      —— 按**支付日**（paid_dt）归集
    refund_amount —— 按**退款发生日**（refund_dt）归集

为什么不能简单按订单日把三者对齐？
因为跨平台对账时，品牌方关心的是"这一天实际到账多少、退了多少钱"，
把 9 月 20 日下单、9 月 23 日退款的钱记回 9 月 20 日，会导致日对账永远对不平。
三种口径各自成立、各自可解释，才是可交付的报表。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import Integer, cast, delete, func, select
from sqlalchemy.orm import Session

from bdp.models import DwdCsSession, DwdOrder, DwdRefund, DwsShopDay, DwsTenantDay, Shop as DimShop


def _shop_order_agg(session: Session) -> dict[tuple[date, str], dict]:
    """下单口径：按订单创建日归集。"""
    stmt = (
        select(
            DwdOrder.order_dt,
            DwdOrder.shop_id,
            func.count().label("cnt"),
            func.sum(DwdOrder.pay_amount).label("gmv"),
        )
        .where(DwdOrder.is_valid.is_(True), DwdOrder.is_cancelled.is_(False))
        .group_by(DwdOrder.order_dt, DwdOrder.shop_id)
    )
    return {
        (dt, shop_id): {"order_cnt": int(cnt or 0), "order_gmv": float(gmv or 0.0)}
        for dt, shop_id, cnt, gmv in session.execute(stmt).all()
    }


def _shop_paid_agg(session: Session) -> dict[tuple[date, str], dict]:
    """支付口径：按支付日归集，与下单日解耦。"""
    stmt = (
        select(
            DwdOrder.paid_dt,
            DwdOrder.shop_id,
            func.count().label("cnt"),
            func.sum(DwdOrder.pay_amount).label("gmv"),
        )
        .where(
            DwdOrder.is_valid.is_(True),
            DwdOrder.paid_dt.is_not(None),
            DwdOrder.order_status != "cancelled",
        )
        .group_by(DwdOrder.paid_dt, DwdOrder.shop_id)
    )
    return {
        (dt, shop_id): {"paid_cnt": int(cnt or 0), "paid_gmv": float(gmv or 0.0)}
        for dt, shop_id, cnt, gmv in session.execute(stmt).all()
    }


def _shop_refund_agg(session: Session) -> dict[tuple[date, str], dict]:
    """退款口径：按退款发生日归集，只计退款成功的记录。"""
    stmt = (
        select(
            DwdRefund.refund_dt,
            DwdRefund.shop_id,
            func.count().label("cnt"),
            func.sum(DwdRefund.refund_amount).label("amt"),
        )
        .where(DwdRefund.is_valid.is_(True), DwdRefund.refund_status == "success")
        .group_by(DwdRefund.refund_dt, DwdRefund.shop_id)
    )
    return {
        (dt, shop_id): {"refund_cnt": int(cnt or 0), "refund_amount": float(amt or 0.0)}
        for dt, shop_id, cnt, amt in session.execute(stmt).all()
    }


def _shop_cs_agg(session: Session) -> dict[tuple[date, str], dict]:
    """客服口径：按会话发起日归集，同时保留分子分母以便下游算比率。"""
    stmt = (
        select(
            DwdCsSession.session_dt,
            DwdCsSession.shop_id,
            func.count().label("cnt"),
            func.sum(cast(DwdCsSession.is_bot, Integer)).label("bot_cnt"),
            func.sum(DwdCsSession.first_response_sec).label("fr_sum"),
            func.sum(cast(DwdCsSession.resolved, Integer)).label("resolved_cnt"),
            func.sum(DwdCsSession.satisfaction).label("sat_sum"),
            func.count(DwdCsSession.satisfaction).label("sat_cnt"),
        )
        .group_by(DwdCsSession.session_dt, DwdCsSession.shop_id)
    )
    return {
        (dt, shop_id): {
            "cs_session_cnt": int(cnt or 0),
            "cs_bot_cnt": int(bot_cnt or 0),
            "cs_first_response_sum": float(fr_sum or 0.0),
            "cs_resolved_cnt": int(resolved_cnt or 0),
            "cs_satisfaction_sum": float(sat_sum or 0.0),
            "cs_satisfaction_cnt": int(sat_cnt or 0),
        }
        for dt, shop_id, cnt, bot_cnt, fr_sum, resolved_cnt, sat_sum, sat_cnt in session.execute(stmt).all()
    }


def build_dws(session: Session) -> dict:
    started = datetime.now()
    session.execute(delete(DwsShopDay))
    session.execute(delete(DwsTenantDay))

    # 店铺维度的唯一权威来源：租户与平台一律从这里取，避免各聚合各写一份导致不一致
    shop_meta: dict[str, tuple[str, str]] = {
        shop_id: (tenant_id, platform)
        for shop_id, tenant_id, platform in session.execute(
            select(DimShop.shop_id, DimShop.tenant_id, DimShop.platform)
        ).all()
    }

    orders = _shop_order_agg(session)
    paids = _shop_paid_agg(session)
    refunds = _shop_refund_agg(session)
    cs = _shop_cs_agg(session)

    keys = set(orders) | set(paids) | set(refunds) | set(cs)
    rows: list[dict] = []
    for dt, shop_id in keys:
        tenant_id, platform = shop_meta.get(shop_id, ("", ""))
        merged = {
            "dt": dt,
            "shop_id": shop_id,
            "tenant_id": tenant_id,
            "platform": platform,
            "order_cnt": 0, "order_gmv": 0.0,
            "paid_cnt": 0, "paid_gmv": 0.0,
            "refund_cnt": 0, "refund_amount": 0.0,
            "cs_session_cnt": 0, "cs_bot_cnt": 0,
            "cs_first_response_sum": 0.0, "cs_resolved_cnt": 0,
            "cs_satisfaction_sum": 0.0, "cs_satisfaction_cnt": 0,
        }
        for part in (orders.get((dt, shop_id)), paids.get((dt, shop_id)),
                     refunds.get((dt, shop_id)), cs.get((dt, shop_id))):
            if part:
                merged.update(part)
        merged["settlement_gmv"] = round(merged["paid_gmv"] - merged["refund_amount"], 2)
        rows.append(merged)

    session.bulk_insert_mappings(DwsShopDay, rows)

    # ---- 租户日汇总 ----
    tenant_agg: dict[tuple[date, str], dict] = defaultdict(
        lambda: {
            "order_cnt": 0, "order_gmv": 0.0, "paid_gmv": 0.0,
            "refund_amount": 0.0, "settlement_gmv": 0.0, "cs_session_cnt": 0,
        }
    )
    for r in rows:
        acc = tenant_agg[(r["dt"], r["tenant_id"])]
        acc["order_cnt"] += r["order_cnt"]
        acc["order_gmv"] += r["order_gmv"]
        acc["paid_gmv"] += r["paid_gmv"]
        acc["refund_amount"] += r["refund_amount"]
        acc["settlement_gmv"] += r["settlement_gmv"]
        acc["cs_session_cnt"] += r["cs_session_cnt"]

    tenant_rows = [
        {
            "dt": dt, "tenant_id": tenant_id,
            "order_cnt": acc["order_cnt"],
            "order_gmv": round(acc["order_gmv"], 2),
            "paid_gmv": round(acc["paid_gmv"], 2),
            "refund_amount": round(acc["refund_amount"], 2),
            "settlement_gmv": round(acc["settlement_gmv"], 2),
            "cs_session_cnt": acc["cs_session_cnt"],
        }
        for (dt, tenant_id), acc in tenant_agg.items()
    ]
    session.bulk_insert_mappings(DwsTenantDay, tenant_rows)
    session.flush()

    return {
        "elapsed_sec": round((datetime.now() - started).total_seconds(), 2),
        "shop_day_rows": len(rows),
        "tenant_day_rows": len(tenant_rows),
        "date_range": (
            f"{min(k[0] for k in keys)} ~ {max(k[0] for k in keys)}" if keys else "-"
        ),
    }
