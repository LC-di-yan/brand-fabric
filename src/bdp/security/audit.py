"""审计日志：租户越权证据链。

合规审计需要一个可独立核查的日志流。"谁在什么时候、以什么身份、
试图访问哪个租户的数据、结果是被允许还是被拒绝"这五项缺一不可。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from bdp.models import AuditLog
from bdp.security.auth import Principal, TenantResolution


def record(
    session: Session,
    *,
    principal: Principal | None,
    action: str,
    resource: str = "",
    resolution: TenantResolution | None = None,
    requested_tenant: str = "",
    allowed: bool = True,
    detail: str = "",
) -> AuditLog:
    entry = AuditLog(
        user_id=principal.user_id if principal else "",
        username=principal.username if principal else "anonymous",
        role=principal.role if principal else "",
        action=action,
        resource=resource,
        requested_tenant=requested_tenant or "",
        effective_tenant=(resolution.effective_tenant or "") if resolution else "",
        allowed=allowed,
        detail=(detail or (resolution.reason if resolution else ""))[:240],
    )
    session.add(entry)
    return entry


def count_violations(session: Session) -> int:
    """越权尝试次数。项目验收要求：真实业务访问 0 次越权成功。"""
    from sqlalchemy import func, select

    stmt = select(func.count()).select_from(AuditLog).where(AuditLog.allowed.is_(False))
    return int(session.execute(stmt).scalar_one())
