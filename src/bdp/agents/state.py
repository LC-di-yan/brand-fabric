"""Agent 运行状态存储：agent_run / agent_task / agent_artifact / agent_event 的 CRUD。

这是多 agent 系统的"消息总线"——用现有数据库实现，不引入 broker：
- 任务表是消息队列（pending → running → 终态）；
- artifact 表是运行级黑板（按 run_id + key 引用）；
- 事件表是进度流。
所有方法使用不设写域守卫的普通会话：agent_* 表属于编排器本身，不属于任何 agent。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from bdp.agents.spec import AgentSpec
from bdp.db import session_context
from bdp.models import AgentArtifact, AgentEvent, AgentRun, AgentTask

TERMINAL_STATUSES = ("succeeded", "degraded", "failed", "skipped", "cancelled")


def _id(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


class StateStore:
    """agent 状态的统一读写入口（编排器与 API 共用）。"""

    # ---- run --------------------------------------------------------------

    def create_run(self, dag_id: str, trigger: str, params: dict) -> str:
        run_id = _id("run")
        with session_context() as s:
            s.add(AgentRun(run_id=run_id, dag_id=dag_id, trigger=trigger,
                           status="running", params=params))
        return run_id

    def finish_run(self, run_id: str, status: str, stats: dict) -> None:
        with session_context() as s:
            run = s.get(AgentRun, run_id)
            if run:
                run.status = status
                run.stats = stats
                run.finished_at = datetime.now()

    def get_run(self, run_id: str) -> AgentRun | None:
        with session_context(commit_on_exit=False) as s:
            return s.get(AgentRun, run_id)

    def list_runs(self, limit: int = 20) -> list[AgentRun]:
        with session_context(commit_on_exit=False) as s:
            return list(
                s.execute(
                    select(AgentRun).order_by(AgentRun.started_at.desc()).limit(limit)
                ).scalars().all()
            )

    # ---- task -------------------------------------------------------------

    def enqueue_task(
        self,
        run_id: str,
        *,
        name: str,
        agent: str,
        spec: AgentSpec,
        deps: list[str],
        params: dict,
        input_payload: dict,
        tenant_scope: str | None,
        lock_keys: list[str],
        parent_task: str | None = None,
        group: str | None = None,
    ) -> str:
        task_id = _id("task")
        with session_context() as s:
            s.add(
                AgentTask(
                    task_id=task_id, run_id=run_id, name=name, agent=agent,
                    deps=deps, parent_task=parent_task, group=group,
                    params=params, input=input_payload, tenant_scope=tenant_scope,
                    status="pending", max_attempts=spec.max_attempts,
                    timeout_sec=spec.timeout_sec, write_domains=list(spec.write_domains),
                    lock_keys=lock_keys,
                )
            )
        return task_id

    def list_tasks(self, run_id: str) -> list[AgentTask]:
        with session_context(commit_on_exit=False) as s:
            return list(
                s.execute(
                    select(AgentTask).where(AgentTask.run_id == run_id).order_by(AgentTask.task_id)
                ).scalars().all()
            )

    def get_task(self, task_id: str) -> AgentTask | None:
        with session_context(commit_on_exit=False) as s:
            return s.get(AgentTask, task_id)

    def claim_task(self, task_id: str) -> bool:
        """抢占式领取：只有仍处于 pending 的任务会被置为 running，防双派发。"""
        with session_context() as s:
            res = s.execute(
                update(AgentTask)
                .where(AgentTask.task_id == task_id, AgentTask.status == "pending")
                .values(status="running", attempt=AgentTask.attempt + 1,
                        started_at=datetime.now(), heartbeat_at=datetime.now())
            )
            return res.rowcount == 1

    def finish_task(self, task_id: str, status: str, result: dict, error: str = "",
                    retryable: bool = False) -> None:
        with session_context() as s:
            s.execute(
                update(AgentTask)
                .where(AgentTask.task_id == task_id, AgentTask.status == "running")
                .values(status=status, result=result, error=error[:480], retryable=retryable,
                        finished_at=datetime.now())
            )

    def mark_skipped(self, task_id: str, reason: str) -> None:
        with session_context() as s:
            s.execute(
                update(AgentTask)
                .where(AgentTask.task_id == task_id, AgentTask.status == "pending")
                .values(status="skipped", error=reason[:480], finished_at=datetime.now())
            )

    def update_input(self, task_id: str, input_payload: dict) -> None:
        """领取后回写任务输入（params + 上游 artifact），供 API/CLI 观测。"""
        with session_context() as s:
            s.execute(
                update(AgentTask)
                .where(AgentTask.task_id == task_id)
                .values(input=input_payload)
            )

    def touch_heartbeat(self, task_id: str) -> None:
        with session_context() as s:
            s.execute(
                update(AgentTask)
                .where(AgentTask.task_id == task_id, AgentTask.status == "running")
                .values(heartbeat_at=datetime.now())
            )

    def requeue_task(self, task_id: str, error: str) -> None:
        """重试：回到 pending 等待下次派发（attempt 已在领取时递增）。"""
        with session_context() as s:
            s.execute(
                update(AgentTask)
                .where(AgentTask.task_id == task_id)
                .values(status="pending", error=error[:480], retryable=True)
            )

    def retry_failed_task(self, task_id: str) -> AgentTask | None:
        """人工/API 触发的重试：failed 任务回 pending，attempt 清零重新计数。"""
        with session_context() as s:
            task = s.get(AgentTask, task_id)
            if task is None or task.status not in ("failed", "skipped", "cancelled"):
                return None
            s.execute(
                update(AgentTask)
                .where(AgentTask.task_id == task_id)
                .values(status="pending", attempt=0, error="", finished_at=None)
            )
            s.execute(
                update(AgentRun).where(AgentRun.run_id == task.run_id).values(
                    status="running", finished_at=None
                )
            )
            return task

    def reap_stale_tasks(self, stale_before: datetime) -> list[str]:
        """崩溃恢复：心跳超时的 running 任务判死并回到可重试队列。"""
        with session_context() as s:
            rows = s.execute(
                select(AgentTask).where(
                    AgentTask.status == "running", AgentTask.heartbeat_at < stale_before
                )
            ).scalars().all()
            task_ids = [t.task_id for t in rows]
            if task_ids:
                s.execute(
                    update(AgentTask)
                    .where(AgentTask.task_id.in_(task_ids))
                    .values(status="failed", retryable=True,
                            error="heartbeat 超时：进程疑似崩溃，任务判死待重试",
                            finished_at=datetime.now())
                )
            return task_ids

    # ---- artifact / event -------------------------------------------------

    def put_artifact(self, run_id: str, key: str, value: dict, producer_task: str) -> None:
        with session_context() as s:
            existing = s.execute(
                select(AgentArtifact).where(
                    AgentArtifact.run_id == run_id, AgentArtifact.key == key
                )
            ).scalar_one_or_none()
            if existing:
                existing.value = value
                existing.producer_task = producer_task
            else:
                s.add(AgentArtifact(run_id=run_id, key=key, value=value,
                                    producer_task=producer_task))

    def get_artifacts(self, run_id: str) -> dict[str, Any]:
        with session_context(commit_on_exit=False) as s:
            rows = s.execute(
                select(AgentArtifact).where(AgentArtifact.run_id == run_id)
            ).scalars().all()
            return {r.key: r.value for r in rows}

    def append_event(self, run_id: str, task_id: str, level: str, message: str,
                     data: dict | None = None) -> None:
        with session_context() as s:
            s.add(AgentEvent(run_id=run_id, task_id=task_id or "", level=level,
                             message=message[:240], data=data or {}))

    def list_events(self, run_id: str, limit: int = 200) -> list[AgentEvent]:
        with session_context(commit_on_exit=False) as s:
            return list(
                s.execute(
                    select(AgentEvent).where(AgentEvent.run_id == run_id)
                    .order_by(AgentEvent.ts.desc()).limit(limit)
                ).scalars().all()
            )

    def prune_events(self, before: datetime) -> int:
        """按保留期清理事件（防止 agent_event 无限膨胀）。"""
        from sqlalchemy import delete

        with session_context() as s:
            res = s.execute(delete(AgentEvent).where(AgentEvent.ts < before))
            return res.rowcount or 0
