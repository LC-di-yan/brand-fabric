"""0003: worker 租约 / goal 编排 / 审批闸口

Revision ID: 0003_agent_evolution
Revisen: 0002_rag_columns
Create Date: 2026-09-28

- agent_task: worker_id / lease_expires_at（P0 租约派发）+ approved_by（P2 审批）
- agent_run:  goal / plan（P1 Planner 溯源）
- agent_memory 表（P3 记忆层）

幂等说明：0001 基线 create_all 已包含当前 models 的全部列/表，
全新库上这些对象已存在——增量迁移必须先探测再添加，否则新库
`alembic upgrade head` 会在本迁移处 duplicate column。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_agent_evolution"
down_revision = "0002_rag_columns"
branch_labels = None
depends_on = None


def _existing_columns(table: str) -> set[str]:
    from sqlalchemy import inspect

    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def _table_exists(table: str) -> bool:
    from sqlalchemy import inspect

    return inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    task_cols = _existing_columns("agent_task")
    with op.batch_alter_table("agent_task") as batch:
        if "worker_id" not in task_cols:
            batch.add_column(sa.Column("worker_id", sa.String(64), nullable=False,
                                       server_default=""))
        if "lease_expires_at" not in task_cols:
            batch.add_column(sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
        if "approved_by" not in task_cols:
            batch.add_column(sa.Column("approved_by", sa.String(32), nullable=False,
                                       server_default=""))

    run_cols = _existing_columns("agent_run")
    with op.batch_alter_table("agent_run") as batch:
        if "goal" not in run_cols:
            batch.add_column(sa.Column("goal", sa.Text(), nullable=False,
                                       server_default=""))
        if "plan" not in run_cols:
            batch.add_column(sa.Column("plan", sa.JSON(), nullable=False,
                                       server_default="{}"))

    if not _table_exists("agent_memory"):
        op.create_table(
            "agent_memory",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("scope", sa.String(24), nullable=False),
            sa.Column("scope_id", sa.String(64), nullable=False),
            sa.Column("key", sa.String(64), nullable=False),
            sa.Column("value", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("tenant_id", sa.String(32), nullable=True),
            sa.Column("produced_by", sa.String(48), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("scope", "scope_id", "key", name="uq_agent_memory"),
        )
    op.create_index("ix_agent_memory_scope", "agent_memory", ["scope", "scope_id"],
                    if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_agent_memory_scope", table_name="agent_memory")
    op.drop_table("agent_memory")
    with op.batch_alter_table("agent_run") as batch:
        batch.drop_column("plan")
        batch.drop_column("goal")
    with op.batch_alter_table("agent_task") as batch:
        batch.drop_column("approved_by")
        batch.drop_column("lease_expires_at")
        batch.drop_column("worker_id")

