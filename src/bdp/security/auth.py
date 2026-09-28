"""鉴权与租户隔离 —— 本项目最关键的安全模块。

为什么单独抽一层
----------------
多品牌代运营场景下，品牌之间是竞争关系，数据隔离是合规底线而非技术优化项。
如果只在"某个接口里顺手加个 where tenant_id=..."，那么缓存、导出、日志、
批量任务里迟早会漏一处，而这种泄漏是不可逆的。

因此这里把规则收敛成三条，全项目只允许通过本模块获取租户上下文：

1. 请求体中的 tenant_id 一律不可信。role=brand 的用户只能访问自己的租户，
   哪怕请求里传了别人的 tenant_id，也会被判定为越权并写审计日志。
2. 越权不是静默忽略，而是显式拒绝（403）并留痕，便于合规审计取证。
3. role=ops 可以跨租户访问，但必须显式声明目标租户，不允许"不传就等于看全部"——
   避免误操作导致的全量泄露。
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt

from bdp.config import settings

Role = Literal["admin", "ops", "brand"]

# OWASP 2023 对 PBKDF2-HMAC-SHA256 的推荐迭代次数（60 万）。
# 哈希格式自描述（algo$iterations$salt$hash），旧哈希仍可验证；
# 登录成功时通过 needs_rehash + hash_password 透明升级（见 verify_and_upgrade）。
# 升级路径说明：若引入 argon2-cffi，只需替换 hash_password/verify_password 两个函数，
# 存储格式换前缀即可，调用方零改动——这也是把哈希逻辑收敛在 security 层的原因。
_PBKDF2_ITERATIONS = 600_000
_PBKDF2_ALGO = "sha256"


# ---------------------------------------------------------------------------
# 口令
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """PBKDF2-SHA256 加盐哈希，格式：pbkdf2_sha256$iterations$salt$hash。

    - 每用户 16 字节随机盐（os.urandom），禁止明文/可逆加密；
    - 迭代次数写入哈希串，便于后续平滑提升强度。
    """
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_{_PBKDF2_ALGO}${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo_part, iterations_s, salt_hex, hash_hex = stored.split("$")
        iterations = int(iterations_s)
        salt = bytes.fromhex(salt_hex)
    except (ValueError, AttributeError):
        return False
    algo = algo_part.removeprefix("pbkdf2_")
    dk = hashlib.pbkdf2_hmac(algo, password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk.hex(), hash_hex)


def needs_rehash(stored: str) -> bool:
    """判断存量哈希是否需要升级（迭代次数低于当前目标强度）。"""
    try:
        _, iterations_s, _, _ = stored.split("$")
        return int(iterations_s) < _PBKDF2_ITERATIONS
    except (ValueError, AttributeError):
        return True


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------


def create_access_token(
    *, user_id: str, username: str, role: Role, tenant_id: str | None, token_version: int = 1
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "tenant_id": tenant_id or "",
        "tv": token_version,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


# ---------------------------------------------------------------------------
# 租户上下文
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """当前调用者。tenant_id 为 None 表示平台级身份（admin）。"""

    user_id: str
    username: str
    role: Role
    tenant_id: str | None

    @property
    def is_platform_level(self) -> bool:
        return self.role == "admin"


@dataclass(frozen=True)
class TenantResolution:
    """租户解析结果。allowed=False 时必须拒绝请求并写审计。"""

    allowed: bool
    effective_tenant: str | None  # None 表示平台级聚合（仅 admin）
    reason: str
    is_violation: bool  # True 表示这是一次跨租户越权尝试


def resolve_tenant(principal: Principal, requested_tenant: str | None) -> TenantResolution:
    """把"调用者身份 + 请求的租户"解析成"实际可用的租户"。

    这是全项目唯一允许决定租户口径的地方。
    """
    requested = (requested_tenant or "").strip()

    # admin：平台级视角，可做跨租户聚合（返回 None 表示聚合口径）
    if principal.role == "admin":
        if not requested:
            return TenantResolution(True, None, "admin 平台级聚合视角", False)
        return TenantResolution(True, requested, "admin 指定租户视角", False)

    # brand：只能看自己的租户
    if principal.role == "brand":
        if not principal.tenant_id:
            return TenantResolution(False, None, "brand 账号未绑定租户，拒绝访问", True)
        if requested and requested != principal.tenant_id:
            return TenantResolution(
                False,
                None,
                f"越权尝试：请求租户 {requested} 与账号租户 {principal.tenant_id} 不一致",
                True,
            )
        return TenantResolution(True, principal.tenant_id, "brand 访问自有租户", False)

    # ops：可跨租户，但必须显式声明，避免误看全量
    if principal.role == "ops":
        if not requested:
            return TenantResolution(False, None, "ops 账号必须显式指定 tenant_id", True)
        return TenantResolution(True, requested, "ops 指定租户视角", False)

    return TenantResolution(False, None, f"未知角色 {principal.role}", True)


def build_tenant_filter_sql(tenant_id: str | None, column: str = "tenant_id") -> str:
    """生成强制租户谓词。

    tenant_id 为 None（admin 聚合）时返回恒真条件——这是唯一允许不带租户过滤的场景，
    且只能由 admin 触发。
    """
    if tenant_id is None:
        return "1=1"
    safe = tenant_id.replace("'", "''")
    return f"{column} = '{safe}'"
