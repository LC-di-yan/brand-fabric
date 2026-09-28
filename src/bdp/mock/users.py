"""演示账号生成：admin / ops / 品牌账号三种角色。

从 generator 拆出。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from bdp.mock.brands import BRANDS
from bdp.models import AppUser
from bdp.security.auth import hash_password

def _gen_users(session: Session) -> None:
    """演示账号：三种角色覆盖租户隔离的完整分支。"""
    users = [
        dict(user_id="U000", username="admin", password_hash=hash_password("admin123"),
             role="admin", tenant_id=None, display_name="平台管理员"),
        dict(user_id="U900", username="ops", password_hash=hash_password("ops123"),
             role="ops", tenant_id=None, display_name="运营支持"),
    ]
    for spec in BRANDS:
        users.append(
            dict(
                # 前缀 UB 避免与平台账号编号冲突
                user_id=f"UB{spec.tenant_id[1:]}",
                username=spec.name.lower(),
                password_hash=hash_password(f"{spec.name.lower()}123"),
                role="brand",
                tenant_id=spec.tenant_id,
                display_name=f"{spec.name} 品牌账号",
            )
        )
    session.bulk_insert_mappings(AppUser, users)
