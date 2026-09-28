"""指标服务：指标字典、口径版本、指标查询、看板汇总。

所有接口都经 `tenant_guard`，保证租户过滤由服务端强制注入。
指标查询结果一律回传 `caliber_notice`（口径版本 + 定义 + 生效区间），
这是"口径可追溯"落到接口层的具体做法。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from bdp.api.deps import tenant_guard
from bdp.api.schemas import MetricPoint, MetricQuery, MetricQueryResult
from bdp.db import get_db
from bdp.metrics import registry
from bdp.metrics.engine import MetricError, compute, compute_with_compare
from bdp.models import DwsShopDay, DwsTenantDay, MetricDef, MetricResult, Shop, Tenant
from bdp.pipeline.quality import quality_summary

router = APIRouter(tags=["metrics"])

KPI_CODES = ("GMV_PAID", "GMV_SETTLE", "ORDER_CNT", "REFUND_RATE")
TREND_CODES = ("GMV_ORDER", "GMV_PAID", "REFUND_AMOUNT")
CS_CODES = ("CS_SESSION_CNT", "CS_BOT_CNT", "CS_BOT_RATIO", "CS_FIRST_RESP_AVG", "CS_SATISFACTION_AVG")


def _caliber_notice(definition: MetricDef) -> str:
    eff = definition.effective_from.isoformat()
    end = definition.effective_to.isoformat() if definition.effective_to else "至今"
    return (
        f"口径 {definition.caliber_version}（生效 {eff} ~ {end}）：{definition.definition} "
        f"Owner：{definition.owner}"
    )


@router.get("/v1/metrics", summary="指标字典")
def list_metrics(_: str | None = Depends(tenant_guard("metric.list", "metric_def")),
                 session: Session = Depends(get_db)) -> dict:
    items = registry.list_metrics(session)
    return {"count": len(items), "items": items}


@router.get("/v1/metrics/{metric_code}/versions", summary="某指标的全部口径版本")
def metric_versions(metric_code: str,
                    _: str | None = Depends(tenant_guard("metric.versions", "metric_def")),
                    session: Session = Depends(get_db)) -> dict:
    versions = registry.list_versions(session, metric_code)
    if not versions:
        raise HTTPException(status_code=404, detail=f"指标 {metric_code} 不存在")
    return {"metric_code": metric_code, "versions": versions}


@router.post("/v1/metrics/query", response_model=MetricQueryResult, summary="查询指标")
def query_metric(
    payload: MetricQuery,
    effective_tenant: str | None = Depends(tenant_guard("metric.query", "metric_result")),
    session: Session = Depends(get_db),
) -> MetricQueryResult:
    # 请求体里的 tenant_id 不能覆盖网关解析结果：
    # brand 账号传入其他租户的 id 时，tenant_guard 已经按请求头判定为越权；
    # 这里再校验一次请求体与有效租户是否一致，避免"头是 A、体是 B"的绕过方式。
    if payload.tenant_id and effective_tenant and payload.tenant_id != effective_tenant:
        raise HTTPException(status_code=403, detail="请求体租户与身份租户不一致")

    tenant_id = effective_tenant or payload.tenant_id
    if tenant_id is None:
        # admin 平台级聚合视图：不针对单租户计算，避免把跨品牌数据混成一条曲线
        raise HTTPException(
            status_code=400,
            detail="平台级账号请通过 X-Tenant-Id 指定租户后再查询指标，避免跨品牌数据混合",
        )

    try:
        result = compute_with_compare(
            session, payload.metric_code,
            caliber_version=payload.caliber_version,
            tenant_id=tenant_id, dim_type=payload.dim_type,
            start=payload.start, end=payload.end, compare=payload.compare,
        )
    except MetricError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    definition = registry.get_definition(session, result["metric_code"], result["caliber_version"])
    points = result["points"]
    if payload.dim_value:
        points = [p for p in points if p["dim_value"] == payload.dim_value]

    return MetricQueryResult(
        **{k: result[k] for k in (
            "metric_code", "metric_name", "caliber_version", "unit", "definition",
            "owner", "dim_type", "start", "end", "total", "additive_total", "point_count", "formula",
        )},
        compare=result.get("compare"),
        points=[MetricPoint(**p) for p in points],
        caliber_notice=_caliber_notice(definition) if definition else "",
    )


@router.get("/v1/metrics/export", summary="导出指标数据为 CSV")
def export_metric_csv(
    effective_tenant: str | None = Depends(tenant_guard("metric.export", "metric_result")),
    session: Session = Depends(get_db),
    metric_code: str = Query(default="GMV_PAID"),
    caliber_version: str | None = None,
    dim_type: str = Query(default="tenant"),
    start: date = Query(...),
    end: date = Query(...),
) -> Response:
    """导出 CSV。

    导出的内容与查询一致，且同样受租户约束——导出不等于"把全量数据打包带走"，
    这正是很多报表系统泄漏的路径，必须统一走 tenant_guard。
    """
    if effective_tenant is None:
        raise HTTPException(status_code=400, detail="导出需指定租户，避免跨品牌数据混合")
    try:
        result = compute(
            session, metric_code, caliber_version=caliber_version,
            tenant_id=effective_tenant, dim_type=dim_type, start=start, end=end,
        )
    except MetricError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    lines = ["dt,tenant_id,dim_value,value,metric_code,caliber_version"]
    for p in result["points"]:
        lines.append(
            f"{p['dt']},{p['tenant_id']},{p['dim_value']},{p['value']},"
            f"{result['metric_code']},{result['caliber_version']}"
        )
    filename = f"{metric_code}_{result['caliber_version']}_{start.isoformat()}_{end.isoformat()}.csv"
    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# 看板汇总
# ---------------------------------------------------------------------------


def _daily_series(
    session: Session, metric_code: str, tenant_id: str | None, start: date, end: date
) -> dict[str, float]:
    """从物化表读日序列；平台级账号按日跨租户求和（仅用于总量趋势，不做品牌对比）。"""
    stmt = select(MetricResult.dt, MetricResult.value).where(
        MetricResult.metric_code == metric_code,
        MetricResult.dim_type == "tenant",
        MetricResult.dt >= start,
        MetricResult.dt <= end,
    )
    if tenant_id:
        stmt = stmt.where(MetricResult.tenant_id == tenant_id)

    acc: dict[str, float] = defaultdict(float)
    for dt, value in session.execute(stmt).all():
        acc[dt.isoformat()] += float(value or 0.0)
    return dict(acc)


@router.get("/v1/dashboard/summary", summary="看板汇总数据")
def dashboard_summary(
    effective_tenant: str | None = Depends(tenant_guard("dashboard.summary", "dws_tenant_day")),
    session: Session = Depends(get_db),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
) -> dict:
    # 默认取有数据的最后 30 天
    bounds = session.execute(
        select(DwsTenantDay.dt).order_by(DwsTenantDay.dt)
    ).scalars().all()
    if not bounds:
        raise HTTPException(status_code=400, detail="没有汇总数据，请先执行 pipeline 与 metrics")
    end = end or bounds[-1]
    start = start or (end.fromordinal(max(end.toordinal() - 29, bounds[0].toordinal())))

    # KPI、趋势、客服三组指标都要取数，去重后统一查询
    all_codes = list(dict.fromkeys(KPI_CODES + TREND_CODES + CS_CODES))
    series = {code: _daily_series(session, code, effective_tenant, start, end) for code in all_codes}
    dates = sorted({d for s in series.values() for d in s})

    def total_of(code: str) -> float:
        # 可加指标求和；比率/均值类用最近一日值 + 区间重算（这里用区间均值近似展示）
        values = series[code]
        if not values:
            return 0.0
        if code in ("REFUND_RATE", "CS_BOT_RATIO"):
            return round(sum(values.values()) / len(values), 4)
        if code in ("CS_FIRST_RESP_AVG", "CS_SATISFACTION_AVG"):
            return round(sum(values.values()) / len(values), 4)
        return round(sum(values.values()), 2)

    kpis = []
    for code in all_codes:
        definition = registry.resolve_version(session, code, end)
        if definition is None:
            continue
        kpis.append(
            {
                "metric_code": code,
                "metric_name": definition.metric_name,
                "unit": definition.unit,
                "caliber_version": definition.caliber_version,
                "value": total_of(code),
                "owner": definition.owner,
            }
        )

    # 品牌对比（平台级/运营可见全部租户；品牌账号只有自己）
    tenant_rows = session.execute(
        select(Tenant.tenant_id, Tenant.name, Tenant.category_l1)
        .order_by(Tenant.tenant_id)
    ).all()
    if effective_tenant:
        tenant_rows = [r for r in tenant_rows if r[0] == effective_tenant]

    compare = []
    for tenant_id, name, category in tenant_rows:
        agg = session.execute(
            select(
                DwsTenantDay.paid_gmv, DwsTenantDay.order_gmv, DwsTenantDay.refund_amount,
                DwsTenantDay.order_cnt, DwsTenantDay.cs_session_cnt, DwsTenantDay.settlement_gmv,
            ).where(
                DwsTenantDay.tenant_id == tenant_id,
                DwsTenantDay.dt >= start,
                DwsTenantDay.dt <= end,
            )
        ).all()
        paid = sum(float(r[0] or 0) for r in agg)
        order_gmv = sum(float(r[1] or 0) for r in agg)
        refund = sum(float(r[2] or 0) for r in agg)
        orders = sum(int(r[3] or 0) for r in agg)
        cs = sum(int(r[4] or 0) for r in agg)
        settle = sum(float(r[5] or 0) for r in agg)
        compare.append(
            {
                "tenant_id": tenant_id,
                "name": name,
                "category": category,
                "order_gmv": round(order_gmv, 2),
                "paid_gmv": round(paid, 2),
                "settlement_gmv": round(settle, 2),
                "refund_amount": round(refund, 2),
                "refund_rate": round(refund / paid, 4) if paid else 0.0,
                "order_cnt": orders,
                "cs_session_cnt": cs,
                "aov": round(paid / orders, 2) if orders else 0.0,
            }
        )

    # 平台维度拆分
    platform_rows = session.execute(
        select(
            DwsShopDay.platform,
            DwsShopDay.shop_id,
            DwsShopDay.order_gmv,
            DwsShopDay.paid_gmv,
            DwsShopDay.refund_amount,
            DwsShopDay.order_cnt,
        ).where(DwsShopDay.dt >= start, DwsShopDay.dt <= end)
    ).all()
    platform_acc: dict[str, dict] = defaultdict(
        lambda: {"paid_gmv": 0.0, "order_gmv": 0.0, "refund_amount": 0.0, "order_cnt": 0, "shops": set()}
    )
    for platform, shop_id, order_gmv, paid, refund, orders in platform_rows:
        if effective_tenant:
            shop_tenant = session.execute(
                select(Shop.tenant_id).where(Shop.shop_id == shop_id)
            ).scalar()
            if shop_tenant != effective_tenant:
                continue
        acc = platform_acc[platform]
        acc["paid_gmv"] += float(paid or 0)
        acc["order_gmv"] += float(order_gmv or 0)
        acc["refund_amount"] += float(refund or 0)
        acc["order_cnt"] += int(orders or 0)
        acc["shops"].add(shop_id)

    platforms = [
        {
            "platform": platform,
            "paid_gmv": round(v["paid_gmv"], 2),
            "refund_rate": round(v["refund_amount"] / v["paid_gmv"], 4) if v["paid_gmv"] else 0.0,
            "order_cnt": v["order_cnt"],
            "shop_count": len(v["shops"]),
        }
        for platform, v in sorted(platform_acc.items(), key=lambda kv: -kv[1]["paid_gmv"])
    ]

    dq = quality_summary(session)
    latest_caliber = {
        code: (registry.resolve_version(session, code, end).caliber_version
               if registry.resolve_version(session, code, end) else None)
        for code in ("GMV_PAID",)
    }

    return {
        "scope": {
            "tenant_id": effective_tenant or "ALL",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": len(dates),
        },
        "kpis": kpis,
        "trend": {
            "dates": dates,
            "series": [
                {"metric_code": code, "points": [series[code].get(d, 0.0) for d in dates]}
                for code in ("GMV_ORDER", "GMV_PAID", "REFUND_AMOUNT")
            ],
            "cs_series": [
                {"metric_code": code, "points": [series[code].get(d, 0.0) for d in dates]}
                for code in CS_CODES
            ],
        },
        "tenant_compare": compare,
        "platforms": platforms,
        "data_quality": {
            "run_id": dq["run_id"],
            "rules": dq["rules"],
            "failed_rules": dq["failed_rules"],
            "overall_pass_rate": dq["overall_pass_rate"],
            "details": dq["details"],
        },
        "caliber": {
            "gmv_paid_effective_version": latest_caliber["GMV_PAID"],
            "note": "本页所有金额指标按支付口径归集；口径版本随数据一并展示，历史对比请先对齐口径。",
        },
    }
