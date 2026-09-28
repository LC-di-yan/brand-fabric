"""baseline: 全部 26 张表的初始基线

Revision ID: 0001_baseline
Revisen:
Create Date: 2026-09-28

用法：
- 全新数据库：alembic upgrade head（建全部表 + alembic_version）
- 已经由 python -m bdp.cli init 建过表的存量库：alembic upgrade head
  （create_all(checkfirst=True) 跳过已存在的表，只补 alembic_version 戳）
- 此后的 schema 变更：alembic revision --autogenerate -m "..." → 检查 → upgrade head
  render_as_batch 已在 env.py 开启，SQLite 下也能安全做 ALTER。
"""

from __future__ import annotations

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from bdp import models  # noqa: F401
    from bdp.models import Base

    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # 基线不可回退：downgrade 到"没有表"只应通过 drop_all 显式执行
    # （生产环境禁止误触发），这里选择显式报错而不是静默删表。
    raise NotImplementedError("baseline 迁移不支持 downgrade；如需重建请使用 init --drop（仅限本地）")
