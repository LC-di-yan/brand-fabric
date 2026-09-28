"""运维与合规接口：审计日志、数据质量、系统健康。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Integer, desc, func, select, text
from sqlalchemy.orm import Session

from bdp.api.deps import require_roles, tenant_guard
from bdp.config import settings
from bdp.db import get_db
from bdp.kb.store import build_store
from bdp.models import (
    AuditLog,
    DqResult,
    DqRule,
    DwdCsSession,
    DwdOrder,
    DwdRefund,
    KbChunk,
    KbDocument,
    MetricResult,
    PlatformSkuMap,
    Shop,
    Sku,
    Spu,
    Tenant,
)
from bdp.pipeline.lineage import full_lineage, metric_lineage, table_lineage, to_dict
from bdp.pipeline.quality import quality_summary
from bdp.security.auth import Principal

router = APIRouter(tags=["admin"])


@router.get("/v1/admin/lineage/tables", summary="表级血缘（由 DAG 与任务读写声明编译）")
def lineage_tables(
    _: str | None = Depends(require_roles("admin", "ops")),
) -> dict:
    return to_dict(table_lineage())


@router.get("/v1/admin/lineage/metrics", summary="指标级血缘（来源表 → 基础指标 → 派生指标）")
def lineage_metrics(
    _: str | None = Depends(require_roles("admin", "ops")),
    session: Session = Depends(get_db),
) -> dict:
    return to_dict(metric_lineage(session))


@router.get("/v1/admin/lineage", summary="全链路血缘（表级 + 指标级合并视图）")
def lineage_full(
    _: str | None = Depends(require_roles("admin", "ops")),
    session: Session = Depends(get_db),
) -> dict:
    return to_dict(full_lineage(session))


@router.get("/v1/admin/selfcheck", summary="组件级健康自检（含修复指引）")
def selfcheck(_: str | None = Depends(tenant_guard("admin.selfcheck", "system"))) -> dict:
    from bdp.bootstrap import run_self_check

    return run_self_check().to_dict()


@router.get("/v1/admin/health", summary="系统健康与数据规模")
def health(
    _: str | None = Depends(tenant_guard("admin.health", "system")),
    session: Session = Depends(get_db),
) -> dict:
    def count(model) -> int:
        return int(session.execute(select(func.count()).select_from(model)).scalar_one())

    try:
        vector = build_store().describe()
    except Exception as exc:
        vector = {"ok": False, "error": str(exc)}

    return {
        "mode": settings.mode,
        "database": "sqlite" if settings.is_sqlite else "postgresql",
        "embedding_backend": settings.embedding_backend,
        "vector_store": vector,
        "rows": {
            "tenants": count(Tenant),
            "shops": count(Shop),
            "spus": count(Spu),
            "skus": count(Sku),
            "platform_sku_map": count(PlatformSkuMap),
            "dwd_order": count(DwdOrder),
            "dwd_refund": count(DwdRefund),
            "dwd_cs_session": count(DwdCsSession),
            "metric_result": count(MetricResult),
            "kb_documents": count(KbDocument),
            "kb_chunks": count(KbChunk),
        },
    }


@router.get("/v1/admin/audit", summary="审计日志（租户越权证据链）")
def audit_logs(
    principal: Principal = Depends(require_roles("admin", "ops")),
    session: Session = Depends(get_db),
    limit: int = Query(default=50, le=500),
    only_denied: bool = False,
) -> dict:
    stmt = select(AuditLog).order_by(desc(AuditLog.id)).limit(limit)
    if only_denied:
        stmt = stmt.where(AuditLog.allowed.is_(False))
    rows = session.execute(stmt).scalars().all()

    denied_total = int(
        session.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.allowed.is_(False))
        ).scalar_one()
    )
    total = int(session.execute(select(func.count()).select_from(AuditLog)).scalar_one())

    return {
        "viewer": principal.username,
        "total_entries": total,
        "denied_entries": denied_total,
        "items": [
            {
                "ts": r.ts.isoformat(),
                "username": r.username,
                "role": r.role,
                "action": r.action,
                "resource": r.resource,
                "requested_tenant": r.requested_tenant,
                "effective_tenant": r.effective_tenant,
                "allowed": r.allowed,
                "detail": r.detail,
            }
            for r in rows
        ],
    }


@router.get("/v1/admin/data-quality", summary="数据质量校验结果")
def data_quality(
    _: str | None = Depends(tenant_guard("admin.dq", "dq_result")),
    session: Session = Depends(get_db),
) -> dict:
    summary = quality_summary(session)
    latest = session.execute(select(func.max(DqResult.run_id))).scalar()
    history = []
    if latest:
        history = [
            {
                "run_id": run_id,
                "checked_at": ts.isoformat(),
                "failed_rules": int(failed or 0),
                "pass_rate": rate,
            }
            for run_id, ts, failed, rate in session.execute(
                select(
                    DqResult.run_id,
                    func.max(DqResult.checked_at),
                    func.sum(func.cast(DqResult.failed_rows > 0, Integer)),
                    func.avg(DqResult.pass_rate),
                ).group_by(DqResult.run_id).order_by(desc(func.max(DqResult.checked_at))).limit(10)
            ).all()
        ]
    return {"summary": summary, "history": history}


@router.get("/v1/admin/data-quality/{rule_id}/samples", summary="查看某条规则失败的行样本")
def data_quality_samples(
    rule_id: str,
    effective_tenant: str | None = Depends(tenant_guard("admin.dq.samples", "dq_rule")),
    session: Session = Depends(get_db),
    limit: int = Query(default=10, le=50),
) -> dict:
    """返回失败行的样本，用于定位问题根因。

    只展示通过率不够的"失败行"，不返回全量数据 —— 数据治理的可视化也应当最小化暴露。
    """
    rule = session.execute(select(DqRule).where(DqRule.rule_id == rule_id)).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail=f"规则 {rule_id} 不存在")

    table = rule.table_name
    if rule.rule_type == "unique":
        # 唯一性规则：找出出现重复的值
        sql = f"""
            SELECT {rule.column_name}, COUNT(*) AS dup_cnt
            FROM {table}
            GROUP BY {rule.column_name}
            HAVING COUNT(*) > 1
            LIMIT :limit
        """
        rows = session.execute(text(sql), {"limit": limit}).mappings().all()
        return {
            "rule_id": rule_id, "table": table, "type": "unique",
            "description": rule.description, "count": len(rows),
            "samples": [dict(r) for r in rows],
        }

    sql = f"SELECT * FROM {table} WHERE NOT ({rule.expression}) LIMIT :limit"
    rows = session.execute(text(sql), {"limit": limit}).mappings().all()
    samples = []
    for row in rows:
        item = dict(row)
        # 租户过滤：brand 账号只能看自己租户的失败样本
        if effective_tenant and item.get("tenant_id") and item["tenant_id"] != effective_tenant:
            continue
        samples.append(item)

    return {
        "rule_id": rule_id, "table": table, "type": rule.rule_type,
        "description": rule.description, "count": len(samples),
        "samples": samples,
        "note": "仅展示失败样本；brand 账号只能看到自己租户的样本。",
    }
