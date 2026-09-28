"""0002: insight_log 增加 RAG 会话与置信列

Revision ID: 0002_rag_columns
Revisen: 0001_baseline
Create Date: 2026-09-28

- thread_id: 多轮会话标识（rag/session.py 指代消解）
- strategy / confidence: Agentic RAG 管线模式与检索置信

幂等说明：0001 基线用 create_all（当前 models 元数据）建表，
全新库上这些列已经存在——增量迁移必须先探测再添加，
否则新库 `alembic upgrade head` 会在 0002 处 duplicate column。
SQLite 走 batch 模式（env.py 已配 render_as_batch）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_rag_columns"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def _existing_columns(table: str) -> set[str]:
    from sqlalchemy import inspect

    bind = op.get_bind()
    return {c["name"] for c in inspect(bind).get_columns(table)}


def _existing_indexes(table: str) -> set[str]:
    from sqlalchemy import inspect

    bind = op.get_bind()
    return {ix["name"] for ix in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    cols = _existing_columns("insight_log")
    idx = _existing_indexes("insight_log")
    with op.batch_alter_table("insight_log") as batch:
        if "thread_id" not in cols:
            batch.add_column(sa.Column("thread_id", sa.String(48), nullable=False,
                                       server_default=""))
        if "ix_insight_log_thread_id" not in idx:
            batch.create_index("ix_insight_log_thread_id", ["thread_id"])
        if "strategy" not in cols:
            batch.add_column(sa.Column("strategy", sa.String(16), nullable=False,
                                       server_default="single"))
        if "confidence" not in cols:
            batch.add_column(sa.Column("confidence", sa.String(8), nullable=False,
                                       server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("insight_log") as batch:
        batch.drop_column("confidence")
        batch.drop_column("strategy")
        batch.drop_index("ix_insight_log_thread_id")
        batch.drop_column("thread_id")
