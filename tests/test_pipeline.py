"""数仓分层与数据质量测试。

重点验证两件事：
    ① 模拟数据里**故意注入的脏数据确实被捕获**（否则"数据质量"只是口号）
    ② 分层之间的数据是一致的（dws 的结算 GMV = 支付 GMV − 退款金额）
"""

from __future__ import annotations

from sqlalchemy import func, select, text

from bdp.db import session_scope
from bdp.models import DwdCsSession, DwdOrder, DwdRefund, DqResult, DwsShopDay, DwsTenantDay, RawOrder

from .conftest import auth


def test_layers_are_populated(seeded):
    with session_scope() as session:
        assert session.execute(select(func.count()).select_from(RawOrder)).scalar_one() > 0
        assert session.execute(select(func.count()).select_from(DwdOrder)).scalar_one() > 0
        assert session.execute(select(func.count()).select_from(DwdRefund)).scalar_one() > 0
        assert session.execute(select(func.count()).select_from(DwdCsSession)).scalar_one() > 0
        assert session.execute(select(func.count()).select_from(DwsShopDay)).scalar_one() > 0
        assert session.execute(select(func.count()).select_from(DwsTenantDay)).scalar_one() > 0


def test_dirty_orders_are_flagged_not_dropped(seeded):
    """脏数据必须保留并打标记：直接丢弃会导致"报表对不上却查不出原因"。"""
    with session_scope() as session:
        raw_count = session.execute(select(func.count()).select_from(RawOrder)).scalar_one()
        dwd_count = session.execute(select(func.count()).select_from(DwdOrder)).scalar_one()
        assert raw_count == dwd_count, "明细层不应丢行"

        invalid = session.execute(
            select(DwdOrder.invalid_reason, func.count())
            .where(DwdOrder.is_valid.is_(False))
            .group_by(DwdOrder.invalid_reason)
        ).all()
        reasons = dict(invalid)
    assert reasons, "模拟数据注入了脏数据，明细层应当有被标记的记录"
    assert any(r in reasons for r in ("pay_amount_null", "pay_amount_not_positive"))


def test_quality_rules_catch_injected_problems(seeded):
    """验证质量规则真的能拦住脏数据。

    分两步：① 内置规则必须都执行过；② 显式插入"必然违规"的记录后再跑一次，
    确认失败行数上升。

    为什么不只依赖模拟数据的随机注入：注入率是 0.2%~0.3%，小样本下可能一次都没触发，
    断言会变成"看运气"。显式构造才能让这条用例稳定地证明规则有效。
    """
    from datetime import datetime

    from bdp.models import RawOrder, RawRefund
    from bdp.pipeline.quality import run_quality_checks

    with session_scope() as session:
        rows = session.execute(
            select(DqResult.rule_id, DqResult.failed_rows, DqResult.pass_rate)
            .where(DqResult.run_id == select(func.max(DqResult.run_id)).scalar_subquery())
        ).all()
        baseline = {r[0]: r[1] for r in rows}
        assert baseline, "应当有质量校验结果"
        assert {"DQ001", "DQ002", "DQ004"} <= set(baseline)

        # 取一条已有订单，构造一条退款额远超订单的记录
        existing = session.execute(select(RawOrder).limit(1)).scalar_one()
        now = datetime(2026, 9, 20, 12, 0, 0)

        session.add(RawOrder(
            order_line_id="TEST-DIRTY-1", tenant_id=existing.tenant_id, shop_id=existing.shop_id,
            platform=existing.platform, order_id="TEST-DIRTY-ORD", platform_sku_code=existing.platform_sku_code,
            qty=1, pay_amount=None, discount=0.0, freight=0.0,
            order_status="paid", is_presale=False, created_at=now, paid_at=now, ingest_batch="TEST",
        ))
        session.add(RawOrder(
            order_line_id="TEST-DIRTY-2", tenant_id=existing.tenant_id, shop_id=existing.shop_id,
            platform=existing.platform, order_id="TEST-DIRTY-ORD-2", platform_sku_code=existing.platform_sku_code,
            qty=1, pay_amount=-88.0, discount=0.0, freight=0.0,
            order_status="paid", is_presale=False, created_at=now, paid_at=now, ingest_batch="TEST",
        ))
        session.add(RawRefund(
            refund_id="TEST-DIRTY-R1", tenant_id=existing.tenant_id, shop_id=existing.shop_id,
            platform=existing.platform, order_id=existing.order_id,
            platform_sku_code=existing.platform_sku_code, refund_amount=9_999_999.0,
            refund_type="refund", reason_code="quality_issue", refund_status="success",
            created_at=now, ingest_batch="TEST",
        ))
        session.flush()

        after_rows = run_quality_checks(session)
        after = {r["rule_id"]: r["failed_rows"] for r in after_rows}

    assert after["DQ001"] >= baseline["DQ001"] + 1, "金额为空未被捕获"
    assert after["DQ002"] >= baseline["DQ002"] + 1, "金额为负未被捕获"
    assert after["DQ004"] >= baseline["DQ004"] + 1, "退款金额超过订单金额未被捕获"

    for rule_id, failed, pass_rate in [
        (r[0], r[1], r[2]) for r in rows
    ]:
        assert 0.0 <= pass_rate <= 1.0
        if failed == 0:
            assert pass_rate == 1.0


def test_dws_settlement_equals_paid_minus_refund(seeded):
    with session_scope() as session:
        rows = session.execute(
            select(DwsShopDay.paid_gmv, DwsShopDay.refund_amount, DwsShopDay.settlement_gmv)
        ).all()
    assert rows
    for paid, refund, settle in rows:
        assert abs((paid - refund) - settle) < 0.02, "结算 GMV 必须等于支付 GMV 减退款金额"


def test_tenant_day_equals_sum_of_shop_day(seeded):
    with session_scope() as session:
        shop_sum = dict(
            session.execute(
                select(DwsShopDay.tenant_id, func.sum(DwsShopDay.paid_gmv))
                .where(text("1=1"))
                .group_by(DwsShopDay.tenant_id)
            ).all()
        )
        tenant_sum = dict(
            session.execute(
                select(DwsTenantDay.tenant_id, func.sum(DwsTenantDay.paid_gmv)).group_by(
                    DwsTenantDay.tenant_id
                )
            ).all()
        )
    for tenant_id, value in tenant_sum.items():
        assert abs(value - shop_sum.get(tenant_id, 0.0)) < 0.05, f"{tenant_id} 租户日汇总与店铺日汇总不一致"


def test_cancelled_orders_excluded_from_gmv(seeded):
    with session_scope() as session:
        cancelled_in_dws = session.execute(
            select(func.sum(DwdOrder.pay_amount)).where(
                DwdOrder.is_cancelled.is_(True), DwdOrder.is_valid.is_(True)
            )
        ).scalar() or 0.0
        assert cancelled_in_dws > 0, "模拟数据里应当有已取消订单"

        order_gmv_dwd = session.execute(
            select(func.sum(DwdOrder.pay_amount)).where(
                DwdOrder.is_valid.is_(True), DwdOrder.is_cancelled.is_(False)
            )
        ).scalar() or 0.0
        order_gmv_dws = session.execute(select(func.sum(DwsShopDay.order_gmv))).scalar() or 0.0
    assert abs(order_gmv_dwd - order_gmv_dws) < 0.05, "取消订单不应计入下单 GMV"


def test_duplicate_cs_sessions_are_deduplicated(seeded):
    """模拟数据注入了重复会话，明细层应按 session_id 去重。"""
    with session_scope() as session:
        total = session.execute(select(func.count()).select_from(DwdCsSession)).scalar_one()
        distinct = session.execute(
            select(func.count(func.distinct(DwdCsSession.session_id)))
        ).scalar_one()
    assert total == distinct


def test_api_health_reports_row_counts(client, tokens):
    res = client.get("/v1/admin/health", headers=auth(tokens["admin"]))
    assert res.status_code == 200
    rows = res.json()["rows"]
    assert rows["dwd_order"] > 0
    assert rows["kb_chunks"] > 0
    assert rows["metric_result"] > 0


def test_api_data_quality_summary(client, tokens):
    res = client.get("/v1/admin/data-quality", headers=auth(tokens["admin"]))
    assert res.status_code == 200
    summary = res.json()["summary"]
    assert summary["rules"] > 0
    assert summary["failed_rules"] > 0
    assert 0 < summary["overall_pass_rate"] <= 1.0
