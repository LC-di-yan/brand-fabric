"""指标计算引擎。

职责
----
把指标字典里的声明式定义翻译成 SQL 并执行，产出指标结果。

设计要点
--------
1. **租户过滤由引擎强制注入**：所有指标 SQL 都会拼上租户谓词，
   且该谓词只能由 `resolve_tenant` 的结果决定 —— 指标层不接受"外部传入的过滤条件"，
   避免有人绕开服务层直接写查询。
2. **派生指标递归求值**：`AOV_PAID = GMV_PAID / PAID_CNT` 这类公式在字典里声明，
   引擎递归算出依赖项后逐点合并，除零返回 None 而不是抛异常。
3. **口径版本可锁定**：可以显式指定版本复算历史数据，也可以按日期自动解析应生效版本。
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from bdp.metrics import registry
from bdp.models import MetricResult
from bdp.security.auth import build_tenant_filter_sql

# 只允许公式里出现大写标识符、数字与四则运算符，防止字典被污染后注入
_FORMULA_TOKEN = re.compile(r"[A-Z][A-Z0-9_]*")
_FORMULA_SAFE = re.compile(r"^[A-Z0-9_+\-*/(). ]+$")

SUPPORTED_DIM_TYPES = ("tenant", "shop", "platform")


class MetricError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 单指标计算
# ---------------------------------------------------------------------------


def _dep_version(session: Session, dep_code: str, preferred: str | None, as_of: date | None) -> str:
    """依赖指标的口径版本：优先与主指标同版本，否则按日期解析。"""
    if preferred and registry.get_definition(session, dep_code, preferred):
        return preferred
    resolved = registry.resolve_version(session, dep_code, as_of)
    if not resolved:
        raise MetricError(f"依赖指标 {dep_code} 没有可用口径版本")
    return resolved.caliber_version


def _series_of_simple(
    session: Session,
    definition,
    *,
    tenant_id: str | None,
    dim_type: str,
    start: date,
    end: date,
) -> dict[tuple, float]:
    tenant_filter = build_tenant_filter_sql(tenant_id)
    table = definition.source_table
    dt_col = definition.dt_column

    if dim_type == "shop":
        dim_select = ", shop_id AS dim_value"
        dim_group = ", shop_id"
    elif dim_type == "platform":
        dim_select = ", platform AS dim_value"
        dim_group = ", platform"
    elif dim_type == "tenant":
        dim_select = ", tenant_id AS dim_value"
        dim_group = ""
    else:
        raise MetricError(f"不支持的维度类型：{dim_type}")

    sql = f"""
        SELECT {dt_col} AS dt, tenant_id AS tenant_id {dim_select}, {definition.agg_expr} AS value
        FROM {table}
        WHERE ({definition.filter_expr})
          AND ({tenant_filter})
          AND {dt_col} >= :start AND {dt_col} <= :end
        GROUP BY {dt_col}, tenant_id {dim_group}
    """
    rows = session.execute(
        text(sql), {"start": start.isoformat(), "end": end.isoformat()}
    ).all()

    out: dict[tuple, float] = {}
    for dt, t_id, dim_value, value in rows:
        if value is None:
            continue
        out[(str(dt), t_id, str(dim_value))] = float(value)
    return out


def _series_of_derived(
    session: Session,
    definition,
    *,
    caliber_version: str,
    as_of: date | None,
    tenant_id: str | None,
    dim_type: str,
    start: date,
    end: date,
) -> dict[tuple, float]:
    expr = definition.agg_expr.strip()
    if not _FORMULA_SAFE.match(expr):
        raise MetricError(f"派生指标公式含非法字符：{expr}")

    deps = _FORMULA_TOKEN.findall(expr)
    if not deps:
        raise MetricError(f"派生指标未声明依赖：{expr}")

    dep_series: dict[str, dict[tuple, float]] = {}
    for dep in deps:
        ver = _dep_version(session, dep, caliber_version, as_of)
        dep_series[dep] = compute_series_map(
            session, dep, ver, tenant_id=tenant_id, dim_type=dim_type, start=start, end=end
        )

    keys = set().union(*[set(s.keys()) for s in dep_series.values()]) if dep_series else set()

    out: dict[tuple, float] = {}
    for key in keys:
        values = {dep: dep_series[dep].get(key) for dep in deps}
        if any(v is None for v in values.values()):
            continue
        safe_locals = {dep: float(v) for dep, v in values.items() if v is not None}
        try:
            result = eval(expr, {"__builtins__": {}}, safe_locals)  # noqa: S307 表达式已被字符白名单约束
        except ZeroDivisionError:
            continue
        except Exception as exc:  # pragma: no cover - 公式错误应尽早暴露
            raise MetricError(f"派生指标计算失败：{expr} -> {exc}") from exc
        out[key] = float(result)
    return out


def compute_series_map(
    session: Session,
    metric_code: str,
    caliber_version: str,
    *,
    tenant_id: str | None = None,
    dim_type: str = "tenant",
    start: date,
    end: date,
    as_of: date | None = None,
) -> dict[tuple, float]:
    definition = registry.get_definition(session, metric_code, caliber_version)
    if definition is None:
        raise MetricError(f"指标 {metric_code} 不存在口径版本 {caliber_version}")

    if definition.source_table == "derived":
        return _series_of_derived(
            session, definition, caliber_version=caliber_version, as_of=as_of,
            tenant_id=tenant_id, dim_type=dim_type, start=start, end=end,
        )
    return _series_of_simple(
        session, definition, tenant_id=tenant_id, dim_type=dim_type, start=start, end=end
    )


def _total_of_simple(
    session: Session, definition, *, tenant_id: str | None, start: date, end: date
) -> float | None:
    """按整个周期重新聚合，而不是把每日值相加。

    这一点很容易出错：GMV 这类可加指标，日值相加等于周期值；
    但客单价、退款率、满意度均值**不可加**——把 30 天的日均满意度加起来毫无意义。
    因此周期总计一律回到明细重新聚合，保证任何聚合方式下结果都正确。
    """
    tenant_filter = build_tenant_filter_sql(tenant_id)
    sql = (
        f"SELECT {definition.agg_expr} FROM {definition.source_table} "
        f"WHERE ({definition.filter_expr}) AND ({tenant_filter}) "
        f"AND {definition.dt_column} >= :start AND {definition.dt_column} <= :end"
    )
    value = session.execute(
        text(sql), {"start": start.isoformat(), "end": end.isoformat()}
    ).scalar()
    return None if value is None else float(value)


def _total_of_derived(
    session: Session,
    definition,
    *,
    caliber_version: str,
    as_of: date | None,
    tenant_id: str | None,
    start: date,
    end: date,
) -> float | None:
    expr = definition.agg_expr.strip()
    if not _FORMULA_SAFE.match(expr):
        raise MetricError(f"派生指标公式含非法字符：{expr}")
    deps = _FORMULA_TOKEN.findall(expr)
    if not deps:
        raise MetricError(f"派生指标未声明依赖：{expr}")

    values: dict[str, float] = {}
    for dep in deps:
        ver = _dep_version(session, dep, caliber_version, as_of)
        dep_def = registry.get_definition(session, dep, ver)
        if dep_def is None:
            raise MetricError(f"依赖指标 {dep} 不存在口径版本 {ver}")
        if dep_def.source_table == "derived":
            total = _total_of_derived(
                session, dep_def, caliber_version=ver, as_of=as_of,
                tenant_id=tenant_id, start=start, end=end,
            )
        else:
            total = _total_of_simple(
                session, dep_def, tenant_id=tenant_id, start=start, end=end
            )
        if total is None:
            return None
        values[dep] = total

    try:
        return float(eval(expr, {"__builtins__": {}}, values))  # noqa: S307 表达式已被字符白名单约束
    except ZeroDivisionError:
        return None


def _total_of(
    session: Session,
    definition,
    *,
    caliber_version: str,
    as_of: date | None,
    tenant_id: str | None,
    start: date,
    end: date,
) -> float | None:
    if definition.source_table == "derived":
        return _total_of_derived(
            session, definition, caliber_version=caliber_version, as_of=as_of,
            tenant_id=tenant_id, start=start, end=end,
        )
    return _total_of_simple(session, definition, tenant_id=tenant_id, start=start, end=end)


def shift_window(start: date, end: date, kind: str) -> tuple[date, date]:
    """返回对比窗口（同比 / 环比）。"""
    if kind == "mom":
        span = (end - start).days + 1
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=span - 1)
        return prev_start, prev_end
    if kind == "yoy":
        try:
            return date(start.year - 1, start.month, start.day), date(end.year - 1, end.month, end.day)
        except ValueError:
            # 2 月 29 日对比到平年：落到 2 月 28 日
            return date(start.year - 1, start.month, 28), date(end.year - 1, end.month, 28)
    raise MetricError(f"不支持的对比类型：{kind}（可选 mom / yoy）")


def compute_with_compare(
    session: Session,
    metric_code: str,
    *,
    caliber_version: str | None = None,
    as_of: date | None = None,
    tenant_id: str | None = None,
    dim_type: str = "tenant",
    start: date,
    end: date,
    compare: str = "none",
) -> dict:
    """计算指标，并按需返回同比/环比对比。"""
    current = compute(
        session, metric_code, caliber_version=caliber_version, as_of=as_of,
        tenant_id=tenant_id, dim_type=dim_type, start=start, end=end,
    )
    if compare not in ("mom", "yoy"):
        return current

    prev_start, prev_end = shift_window(start, end, compare)
    previous_points: list = []
    prev_total: float | None = None
    note = ""
    try:
        previous = compute(
            session, metric_code, caliber_version=caliber_version, as_of=as_of,
            tenant_id=tenant_id, dim_type=dim_type, start=prev_start, end=prev_end,
        )
        prev_total = previous["total"]
        previous_points = previous["points"]
    except MetricError:
        # 对比窗口超出数据覆盖范围（例如同比到上年但没有上年数据）：不报错，
        # 以 previous_total=None 告知调用方"对比期无数据"，由前端决定如何展示。
        note = "对比窗口无可用数据，无法计算同比/环比"

    delta = None
    delta_pct = None
    cur_total = current["total"]
    if cur_total is not None and prev_total is not None:
        delta = round(cur_total - prev_total, 4)
        if prev_total != 0:
            delta_pct = round(delta / abs(prev_total), 4)

    current["compare"] = {
        "type": compare,
        "previous_start": prev_start.isoformat(),
        "previous_end": prev_end.isoformat(),
        "previous_total": prev_total,
        "delta": delta,
        "delta_pct": delta_pct,
        "previous_points": previous_points,
        "note": note,
    }
    return current


def compute(
    session: Session,
    metric_code: str,
    *,
    caliber_version: str | None = None,
    as_of: date | None = None,
    tenant_id: str | None = None,
    dim_type: str = "tenant",
    start: date,
    end: date,
) -> dict:
    """计算单个指标，返回可直接序列化的结果。"""
    if dim_type not in SUPPORTED_DIM_TYPES:
        raise MetricError(f"不支持的维度类型：{dim_type}")
    if start > end:
        raise MetricError("开始日期不能晚于结束日期")

    if caliber_version:
        definition = registry.get_definition(session, metric_code, caliber_version)
        if definition is None:
            raise MetricError(f"指标 {metric_code} 没有口径版本 {caliber_version}")
    else:
        definition = registry.resolve_version(session, metric_code, as_of or end)
        if definition is None:
            raise MetricError(f"指标 {metric_code} 在 {as_of or end} 没有生效口径")

    series = compute_series_map(
        session, metric_code, definition.caliber_version,
        tenant_id=tenant_id, dim_type=dim_type, start=start, end=end, as_of=as_of,
    )

    points = [
        {"dt": key[0], "tenant_id": key[1], "dim_value": key[2], "value": round(v, 4)}
        for key, v in sorted(series.items())
    ]
    total = _total_of(
        session, definition,
        caliber_version=definition.caliber_version, as_of=as_of,
        tenant_id=tenant_id, start=start, end=end,
    )

    return {
        "metric_code": metric_code,
        "metric_name": definition.metric_name,
        "caliber_version": definition.caliber_version,
        "unit": definition.unit,
        "definition": definition.definition,
        "owner": definition.owner,
        "dim_type": dim_type,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total": round(total, 4) if total is not None else None,
        "additive_total": round(sum(p["value"] for p in points), 4),
        "points": points,
        "point_count": len(points),
        "formula": definition.agg_expr if definition.source_table == "derived" else None,
    }


# ---------------------------------------------------------------------------
# 物化到 metric_result
# ---------------------------------------------------------------------------

MATERIALIZE_TARGETS: tuple[tuple[str, str], ...] = (
    ("GMV_ORDER", "tenant"), ("GMV_ORDER", "shop"),
    ("GMV_PAID", "tenant"), ("GMV_PAID", "shop"),
    ("REFUND_AMOUNT", "tenant"), ("REFUND_AMOUNT", "shop"),
    ("GMV_SETTLE", "tenant"), ("GMV_SETTLE", "shop"),
    ("ORDER_CNT", "tenant"), ("PAID_CNT", "tenant"),
    ("AOV_PAID", "tenant"), ("REFUND_RATE", "tenant"),
    ("CS_SESSION_CNT", "tenant"), ("CS_BOT_CNT", "tenant"),
    ("CS_BOT_RATIO", "tenant"), ("CS_FIRST_RESP_AVG", "tenant"),
    ("CS_SATISFACTION_AVG", "tenant"), ("CS_RESOLVE_RATE", "tenant"),
)


def materialize_tenant(
    session: Session,
    *,
    tenant_id: str,
    start: date,
    end: date,
    targets: tuple[tuple[str, str], ...] = MATERIALIZE_TARGETS,
) -> dict:
    """物化单个租户的指标结果。

    从 materialize 中拆出，供 agent 按租户 fan-out 并行调用：
    每个租户独立计算、独立写入，单租户失败不影响其他租户。
    注意：本函数不做全表清理（那是 materialize 编排层的职责）。
    """
    written = 0
    results = []
    for code, dim in targets:
        try:
            res = compute(session, code, tenant_id=tenant_id, dim_type=dim, start=start, end=end)
        except MetricError as exc:
            results.append({"metric_code": code, "tenant_id": tenant_id, "error": str(exc)})
            continue
        rows = [
            dict(
                metric_code=code, caliber_version=res["caliber_version"],
                tenant_id=tenant_id, dt=date.fromisoformat(p["dt"]),
                dim_type=dim, dim_value=p["dim_value"], value=p["value"],
            )
            for p in res["points"]
        ]
        if rows:
            session.bulk_insert_mappings(MetricResult, rows)
            written += len(rows)
        results.append(
            {
                "metric_code": code, "caliber_version": res["caliber_version"],
                "tenant_id": tenant_id, "dim_type": dim, "points": len(rows), "total": res["total"],
            }
        )
    return {"tenant_id": tenant_id, "written": written, "details": results}


def materialize(
    session: Session,
    *,
    start: date,
    end: date,
    tenant_id: str | None = None,
    targets: tuple[tuple[str, str], ...] = MATERIALIZE_TARGETS,
) -> dict:
    """把指标结果写入 metric_result，供看板与 API 快速读取。

    注意：这里对每个租户分别计算，保证物化表里**每一行都带明确的 tenant_id**，
    不会出现"某行是跨租户聚合"的含糊记录。
    """
    session.execute(delete(MetricResult))

    tenant_ids = (
        [tenant_id]
        if tenant_id
        else [t for (t,) in session.execute(text("SELECT tenant_id FROM dim_tenant ORDER BY tenant_id")).all()]
    )

    written = 0
    results = []
    for t_id in tenant_ids:
        res = materialize_tenant(session, tenant_id=t_id, start=start, end=end, targets=targets)
        written += res["written"]
        results.extend(res["details"])

    session.flush()
    return {"written": written, "targets": len(targets), "tenants": len(tenant_ids), "details": results}
