"""FastAPI 依赖：鉴权、租户解析、审计。

**这是全项目唯一的租户入口**：任何路由要拿到"当前能访问哪个租户"，
都必须经过 `tenant_context`。请求体里传的 tenant_id 只作为"请求意向"，
能否成立由 `resolve_tenant` 决定；越权请求一律 403 + 写审计。

双轨凭证
--------
1. **Bearer JWT**（Authorization 头）：API 客户端与自动化测试使用。
   浏览器不会自动附带该头，天然免疫 CSRF。
2. **HttpOnly 会话 Cookie**：登录页浏览器会话使用。
   状态变更请求（POST/PUT/DELETE/PATCH）额外强制 CSRF 双提交校验：
   请求头 X-CSRF-Token 必须与会话签发时写入的非 HttpOnly csrf cookie 一致，
   且 Origin/Referer（如携带）必须指向本站——跨站脚本读不到 cookie 域外的
   自定义头，第三方站点无法伪造。
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from bdp.config import settings
from bdp.db import get_db
from bdp.models import AppUser
from bdp.security import audit
from bdp.security.auth import (
    Principal,
    TenantResolution,
    decode_access_token,
    resolve_tenant,
    verify_password,
)


def authenticate(session: Session, username: str, password: str) -> AppUser | None:
    # 参数化查询（SQLAlchemy ORM），不存在字符串拼接注入面
    user = session.execute(select(AppUser).where(AppUser.username == username)).scalar_one_or_none()
    if user is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def _extract_token(request: Request, authorization: str | None) -> str | None:
    """凭证提取顺序：Authorization 头优先，其次会话 Cookie。"""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return request.cookies.get(settings.cookie_name)


def _csrf_ok(request: Request) -> bool:
    """CSRF 双提交 + Origin 校验（仅对 Cookie 会话的状态变更请求要求）。"""
    method = request.method.upper()
    if method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return True
    header_token = request.headers.get("X-CSRF-Token") or ""
    cookie_token = request.cookies.get(settings.csrf_cookie_name) or ""
    if not header_token or not cookie_token:
        return False
    if not hmac.compare_digest(header_token, cookie_token):
        return False
    # Origin/Referer 同源校验（浏览器跨站请求必带 Origin；命令行客户端可不带）
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin:
        host = request.url.netloc
        # 允许同源；file:// 场景（本地双击打开）Origin 为 null，由双提交本身兜底
        if not origin.startswith("null") and host not in origin:
            return False
    return True


def get_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_db),
) -> Principal:
    token = _extract_token(request, authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 Bearer Token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_access_token(token)
    except Exception as exc:  # 过期或签名不合法
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效或已过期") from exc

    # Cookie 会话的状态变更请求必须通过 CSRF 校验（Bearer 请求跳过，天然免疫）
    from_cookie = not (authorization and authorization.lower().startswith("bearer "))
    if from_cookie and not _csrf_ok(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验未通过")

    # 凭证版本校验：修改密码后 token_version +1，旧 token 立即失效。
    # 代价是每次请求多一次用户查询——在演示规模下可接受，且这条规则本身是可验证的安全能力。
    user = session.execute(
        select(AppUser).where(AppUser.user_id == str(payload.get("sub") or ""))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="凭证已失效，请重新登录")
    if user.token_version != int(payload.get("tv") or 0):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="凭证已失效，请重新登录",
        )

    return Principal(
        user_id=user.user_id,
        username=user.username,
        role=user.role,
        tenant_id=user.tenant_id,
    )


@dataclass
class TenantContext:
    principal: Principal
    requested_tenant: str | None


def tenant_context(
    principal: Principal = Depends(get_principal),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> TenantContext:
    """从请求头读取"请求意向租户"。

    注意：不用请求体传租户，因为不同接口的请求体结构不同，容易漏改；
    统一走请求头可以让规则收敛在一处。
    """
    return TenantContext(principal=principal, requested_tenant=x_tenant_id)


def tenant_guard(action: str, resource: str = ""):
    """生成一个"解析租户 + 落审计 + 越权即 403"的依赖。

    以工厂形式提供，是为了让每个接口能声明自己的 action/resource，
    避免把审计信息写成固定字符串而失去排查价值。
    """

    def _dependency(
        ctx: TenantContext = Depends(tenant_context),
        session: Session = Depends(get_db),
    ) -> str | None:
        resolution: TenantResolution = resolve_tenant(ctx.principal, ctx.requested_tenant)
        audit.record(
            session,
            principal=ctx.principal,
            action=action,
            resource=resource,
            resolution=resolution,
            requested_tenant=ctx.requested_tenant or "",
            allowed=resolution.allowed,
        )
        session.commit()
        if not resolution.allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=resolution.reason)
        return resolution.effective_tenant

    return _dependency


def require_roles(*roles: str):
    """角色白名单依赖。"""

    def _checker(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"角色 {principal.role} 无权访问该接口",
            )
        return principal

    return _checker
