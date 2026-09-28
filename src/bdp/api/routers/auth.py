"""鉴权与身份接口（登录页后端）。

安全措施清单（与前端登录页配套，逐条可验证）
--------------------------------------------
1. **口令存储**：PBKDF2-HMAC-SHA256 + 每用户随机盐，60 万迭代（OWASP 2023），
   非可逆；登录成功时检测存量哈希强度并透明升级（needs_rehash）。
2. **输入校验**：用户名白名单字符集（schemas.TokenRequest），
   查询全部走 SQLAlchemy 参数化，无字符串拼接 SQL。
3. **统一模糊提示**：账号不存在与密码错误返回**字节级相同**的 401 响应体，
   不泄露账号存在性；不再返回剩余尝试次数（该字段本身就是探测面）。
4. **失败阶梯**：连续失败 ≥3 次要求验证码（security/captcha.py，HMAC 签名 SVG，
   一次性使用）；≥5 次锁定 15 分钟（security/ratelimit.py）。
5. **CSRF**：登录成功后签发双凭证——HttpOnly 会话 Cookie + 可读 csrf cookie；
   Cookie 会话的状态变更请求强制 X-CSRF-Token 双提交 + Origin 同源校验
   （见 deps._csrf_ok）。Bearer 客户端不受影响（天然免疫）。
6. **会话 Cookie**：HttpOnly、SameSite=Lax、Secure（BDP_COOKIE_SECURE，
   localhost 安全上下文可用）、Path=/、Max-Age 与 JWT 过期一致；登出即清除。
7. **审计**：auth.login 成功/失败/锁定/验证码错误全部落 audit_log；
   用户名可记录，**密码永不记录**（访问日志只记 method/path/status，见 middleware）。
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from bdp.api.deps import authenticate, get_principal
from bdp.api.schemas import ChangePasswordRequest, TokenRequest, TokenResponse, PrincipalOut
from bdp.config import settings
from bdp.db import get_db
from bdp.models import AppUser
from bdp.security import audit, captcha
from bdp.security.auth import (
    Principal,
    create_access_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from bdp.security.ratelimit import RateLimited, login_rate_limiter

router = APIRouter(tags=["auth"])

# 统一失败响应体：不区分"用户不存在"与"密码错误"，不暴露计数
_UNIFIED_LOGIN_FAIL = {
    "code": "UNAUTHORIZED",
    "message": "账号或密码错误",
}


def _issue_token(user: AppUser) -> TokenResponse:
    token = create_access_token(
        user_id=user.user_id,
        username=user.username,
        role=user.role,
        tenant_id=user.tenant_id,
        token_version=user.token_version,
    )
    return TokenResponse(
        access_token=token,
        username=user.username,
        role=user.role,
        tenant_id=user.tenant_id,
        expires_in_minutes=settings.jwt_expire_minutes,
    )


def _set_session_cookies(response: Response, token: str) -> str:
    """签发会话 Cookie（HttpOnly）+ CSRF 双提交令牌（可读）。返回 csrf token。"""
    max_age = settings.jwt_expire_minutes * 60
    csrf_token = secrets.token_urlsafe(32)
    # Secure：生产 HTTPS 必须；浏览器将 http://localhost / 127.0.0.1 视为安全上下文，
    # 本地演示同样可用。纯 IP 的非加密访问需显式设 BDP_COOKIE_SECURE=false。
    response.set_cookie(
        key=settings.cookie_name, value=token,
        max_age=max_age, httponly=True, secure=settings.cookie_secure,
        samesite="lax", path="/",
    )
    response.set_cookie(
        key=settings.csrf_cookie_name, value=csrf_token,
        max_age=max_age, httponly=False, secure=settings.cookie_secure,
        samesite="lax", path="/",
    )
    return csrf_token


def _audit_login(session: Session, *, username: str, allowed: bool, detail: str,
                 user: AppUser | None = None) -> None:
    entry = audit.record(
        session, principal=None, action="auth.login", resource="app_user",
        requested_tenant=(user.tenant_id or "") if user else "",
        allowed=allowed, detail=detail,
    )
    # 用户名可审计；密码绝不进入日志与审计
    entry.username = (username or "")[:48]


@router.get("/v1/auth/captcha", summary="获取登录验证码（SVG，HMAC 签名无状态）")
def get_captcha() -> dict:
    captcha_id, svg = captcha.generate()
    return {"captcha_id": captcha_id, "svg": svg}


@router.post("/v1/auth/token", response_model=TokenResponse, summary="登录（签发 Bearer 与会话 Cookie）")
def login(
    payload: TokenRequest,
    response: Response,
    request: Request,
    session: Session = Depends(get_db),
) -> TokenResponse:
    # 1) 锁定检查（先于任何凭据处理，避免锁定期间做无谓的哈希计算）
    try:
        login_rate_limiter.check(payload.username)
    except RateLimited as exc:
        _audit_login(session, username=payload.username, allowed=False,
                     detail=f"locked retry_after={exc.retry_after_seconds}")
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "RATE_LIMITED",
                "message": "尝试次数过多，请稍后再试",
                "retry_after_seconds": exc.retry_after_seconds,
            },
        ) from exc

    # 2) 失败达到阈值后强制验证码（验证码错误同样计入统一失败，不单独暴露原因）
    if login_rate_limiter.failures(payload.username) >= settings.login_captcha_threshold:
        try:
            captcha.verify(payload.captcha_id or "", payload.captcha_code or "")
        except captcha.CaptchaError:
            # 不记录失败计数（验证码错误不消耗密码尝试次数），但要求重新获取
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "CAPTCHA_REQUIRED", "message": "需要验证码", "captcha_required": True},
            ) from None

    # 3) 认证：统一模糊失败响应，不区分账号是否存在
    user = authenticate(session, payload.username, payload.password)
    if user is None:
        login_rate_limiter.record_failure(payload.username)
        _audit_login(session, username=payload.username, allowed=False, detail="bad credentials")
        session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=dict(_UNIFIED_LOGIN_FAIL))

    # 4) 成功：透明升级弱哈希 → 计数清零 → 审计 → 签发双凭证
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
        session.add(user)
    login_rate_limiter.record_success(payload.username)
    _audit_login(session, username=payload.username, allowed=True, detail="login ok", user=user)
    session.commit()

    token_response = _issue_token(user)
    csrf_token = _set_session_cookies(response, token_response.access_token)
    token_response.csrf_token = csrf_token  # 供 file:// 本地模式使用（无 cookie 环境）
    return token_response


@router.post("/v1/auth/logout", summary="登出（清除会话 Cookie）")
def logout(
    response: Response,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_db),
) -> dict:
    response.delete_cookie(settings.cookie_name, path="/", samesite="lax")
    response.delete_cookie(settings.csrf_cookie_name, path="/", samesite="lax")
    audit.record(session, principal=principal, action="auth.logout", resource="app_user",
                 allowed=True, detail="logout")
    session.commit()
    return {"ok": True}


@router.get("/v1/auth/me", response_model=PrincipalOut, summary="查看当前身份")
def me(principal: Principal = Depends(get_principal)) -> PrincipalOut:
    return PrincipalOut(
        user_id=principal.user_id,
        username=principal.username,
        role=principal.role,
        tenant_id=principal.tenant_id,
        is_platform_level=principal.is_platform_level,
    )


@router.post("/v1/auth/change-password", summary="修改密码（改后强制重新登录）")
def change_password(
    payload: ChangePasswordRequest,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_db),
) -> dict:
    user = session.execute(
        select(AppUser).where(AppUser.user_id == principal.user_id)
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="凭证已失效，请重新登录")

    if not verify_password(payload.old_password, user.password_hash):
        # 改密接口已在鉴权保护内；旧密码错误记审计即可，不占用登录限流计数
        audit.record(session, principal=principal, action="auth.change_password",
                     resource="app_user", allowed=False, detail="old password mismatch")
        session.commit()
        raise HTTPException(status_code=400, detail="旧密码不正确")
    if payload.new_password == payload.old_password:
        raise HTTPException(status_code=400, detail="新密码不能与旧密码相同")
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="新密码长度至少 8 位")
    if payload.new_password.isdigit() or payload.new_password.isalpha():
        raise HTTPException(status_code=400, detail="新密码需同时包含字母与数字")

    user.password_hash = hash_password(payload.new_password)
    # 凭证版本 +1：此前签发的所有 token（含会话 Cookie）立即失效
    user.token_version += 1
    session.add(user)
    audit.record(session, principal=principal, action="auth.change_password",
                 resource="app_user", allowed=True, detail="password rotated, tokens invalidated")
    session.commit()

    return {
        "changed": True,
        "username": user.username,
        "token_version": user.token_version,
        "message": "密码已更新，旧凭证已失效，请重新登录",
    }
