"""会话记忆：多轮问答的指代消解上下文。

存储策略
--------
复用现有 `insight_log` 表：按 thread_id 追加，读最近 N 轮。
不建新表——问答留痕已经在那里，会话只是给它加一列；
TTL 不需要（留痕本身就是审计资产，读的时候 LIMIT 即可）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from bdp.config import settings
from bdp.models import InsightLog


def new_thread_id() -> str:
    return f"thd-{uuid.uuid4().hex[:12]}"


def load_history(session: Session, thread_id: str, limit: int | None = None) -> list[dict]:
    """读取会话最近 N 轮（旧→新），供指代消解使用。"""
    if not thread_id:
        return []
    limit = limit or settings.rag_session_turns
    rows = (
        session.execute(
            select(InsightLog)
            .where(InsightLog.thread_id == thread_id)
            .order_by(InsightLog.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        {"question": r.question, "answer": r.answer, "degraded": r.degraded}
        for r in reversed(rows)
    ]
