"""跨 run 记忆（P3 episodic 层）：run 摘要自动沉淀 + agent 显式经验。

与 artifact 的分工
------------------
- artifact（agent_artifact 表）是 run 内黑板：run 结束即失去意义；
- memory（agent_memory 表）是跨 run 沉淀：给"下一次同类目标"提供冷启动上下文。

写权限模型：写入走写域守卫——默认 agent 没有 memory 域，
只有 spec.write_domains 声明了 "memory" 的 agent 才能沉淀（当前没有：
run_summary 由编排器在 run 收尾时自动写，agent 显式沉淀是预留能力）。
读取无权限约束（记忆是去敏的聚合，不含行级业务数据）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from bdp.config import settings
from bdp.db import session_context
from bdp.models import AgentMemory

MEMORY_DOMAIN = "memory"


def remember(scope: str, scope_id: str, key: str, value: dict, *,
             produced_by: str = "", tenant_id: str | None = None,
             ttl_days: int | None = None) -> None:
    """写入或刷新一条记忆（同 scope+scope_id+key 覆盖）。"""
    expires = (
        datetime.now() + timedelta(days=ttl_days if ttl_days is not None else settings.agent_memory_ttl_days)
        if (ttl_days is not None or settings.agent_memory_ttl_days > 0) else None
    )
    with session_context() as s:
        row = s.execute(
            select(AgentMemory).where(
                AgentMemory.scope == scope, AgentMemory.scope_id == scope_id,
                AgentMemory.key == key,
            )
        ).scalar_one_or_none()
        if row:
            row.value = value
            row.produced_by = produced_by
            row.expires_at = expires
        else:
            s.add(AgentMemory(scope=scope, scope_id=scope_id, key=key, value=value,
                              produced_by=produced_by, tenant_id=tenant_id,
                              expires_at=expires))


def recall(scope: str, scope_id: str, key: str) -> dict | None:
    """读取一条记忆；过期视同不存在。"""
    with session_context(commit_on_exit=False) as s:
        row = s.execute(
            select(AgentMemory).where(
                AgentMemory.scope == scope, AgentMemory.scope_id == scope_id,
                AgentMemory.key == key,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        if row.expires_at is not None and row.expires_at < datetime.now():
            return None
        return row.value


def recall_recent(scope: str, limit: int = 3) -> list[dict]:
    """按创建时间倒序取最近 N 条未过期记忆（planner 冷启动注入用）。"""
    with session_context(commit_on_exit=False) as s:
        rows = s.execute(
            select(AgentMemory)
            .where(AgentMemory.scope == scope)
            .order_by(AgentMemory.created_at.desc())
            .limit(limit * 2)
        ).scalars().all()
        now = datetime.now()
        out = []
        for r in rows:
            if r.expires_at is not None and r.expires_at < now:
                continue
            out.append({
                "scope_id": r.scope_id, "key": r.key, "value": r.value,
                "produced_by": r.produced_by, "created_at": r.created_at.isoformat(),
            })
            if len(out) >= limit:
                break
        return out


def summarize_run(run_id: str, dag_id: str, status: str, stats: dict, *,
                  goal: str = "", tenant_id: str | None = None) -> None:
    """run 收尾时的自动沉淀（编排器调用）：失败/降级 run 才值得记住——
    全绿的 run 对下次运行没有参考价值，全部记录只会稀释记忆。"""
    if status == "succeeded":
        return
    remember(
        scope="run_summary",
        scope_id=run_id,
        key=f"{dag_id}:{status}",
        value={
            "dag_id": dag_id, "status": status, "goal": goal,
            "failed_tasks": stats.get("failed_tasks", []),
            "skipped_tasks": stats.get("skipped_tasks", []),
            "degraded_tasks": stats.get("degraded_tasks", []),
        },
        produced_by="orchestrator", tenant_id=tenant_id,
        ttl_days=settings.agent_memory_ttl_days,
    )


def prune_expired() -> int:
    """清理过期记忆（可由 nightly 的收尾顺带调用）。"""
    from sqlalchemy import delete

    with session_context() as s:
        res = s.execute(
            delete(AgentMemory).where(
                AgentMemory.expires_at.is_not(None),
                AgentMemory.expires_at < datetime.now(),
            )
        )
        return res.rowcount or 0
