"""独立 Worker 执行体：从 API 进程分离的编排/执行循环（P0）。

运行模型
--------
- `python -m bdp.worker --id w1`：循环认领 lease 到期未续的任务并执行；
  执行复用 Orchestrator._execute_task（写域守卫/重试/事件流全部一致），
  唯一的区别是派发方式从"进程内线程池"换成"DB 租约认领"。
- inline（默认）模式行为与 V3 完全一致：编排循环在 API/CLI 进程内的线程池里跑。
- process 模式下任务执行权属于 worker，API 进程只负责 prepare + 查询——
  API 重启不再中断编排；多 worker 天然可水平扩（同一 PG 库）。

租约语义（与心跳分层）
----------------------
- 心跳（heartbeat_at）：判"任务卡死"（任务阶段边界更新，V3 已有）；
- 租约（lease_expires_at）：判"worker 死"（worker 周期续租，本模块新增）。
租约过期的 running 任务由 reap_leased_tasks 回收重派——worker 崩了，
任务回到队列而不是永远卡在 running。

优雅停机：SIGTERM/SIGINT 后不再认领新任务，手中任务跑完、租约自然到期释放。
SQLite 下 process 模式被显式拒绝（单写者），inline 是唯一合法形态。
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import select, update

from bdp.agents.dag import DAGS
from bdp.agents.orchestrator import Orchestrator
from bdp.agents.state import StateStore
from bdp.config import settings


def claim_leased_tasks(store: StateStore, worker_id: str, limit: int = 2) -> list:
    """认领：pending 且依赖就绪的任务置为 running 并打上租约。

    依赖检查在认领时做（worker 视角）——依赖未满足的任务留在队列里，
    下一轮循环再看。抢占语义与 inline 的 claim_task 相同（UPDATE...WHERE pending）。
    """
    from bdp.db import session_context
    from bdp.models import AgentTask

    claimed: list[str] = []
    with session_context() as s:
        rows = s.execute(
            select(AgentTask)
            .where(AgentTask.status == "pending")
            .order_by(AgentTask.task_id)
            .limit(limit * 4)
        ).scalars().all()
        for t in rows:
            if len(claimed) >= limit:
                break
            # 依赖检查：deps 中任一依赖未成功则本轮跳过（fan-out 组内子任务只看父任务）
            if t.deps:
                siblings = s.execute(
                    select(AgentTask).where(AgentTask.run_id == t.run_id)
                ).scalars().all()
                if not _deps_satisfied(t, siblings):
                    continue
            res = s.execute(
                update(AgentTask)
                .where(AgentTask.task_id == t.task_id,
                       AgentTask.status == "pending")
                .values(status="running", attempt=AgentTask.attempt + 1,
                        started_at=datetime.now(), heartbeat_at=datetime.now(),
                        worker_id=worker_id,
                        lease_expires_at=datetime.now() + timedelta(seconds=settings.agent_lease_sec))
            )
            if res.rowcount == 1:
                claimed.append(t.task_id)
    return [store.get_task(tid) for tid in claimed]


def _deps_satisfied(task, siblings) -> bool:
    by_name = {t.name: t for t in siblings}
    for dep in task.deps or []:
        dep_task = by_name.get(dep)
        if dep_task is None or dep_task.status != "succeeded":
            # fan-out 组：组内任一成功即视为该依赖满足（与 orchestrator 的组语义一致）
            group_ok = any(s.group == dep and s.status == "succeeded" for s in siblings)
            if not group_ok:
                return False
    return True


def renew_lease(store: StateStore, task_id: str, worker_id: str) -> bool:
    """续租：只有仍属于本 worker 且未终态的任务才续。"""
    from bdp.db import session_context
    from bdp.models import AgentTask

    with session_context() as s:
        res = s.execute(
            update(AgentTask)
            .where(AgentTask.task_id == task_id, AgentTask.worker_id == worker_id,
                   AgentTask.status == "running")
            .values(lease_expires_at=datetime.now() + timedelta(seconds=settings.agent_lease_sec),
                    heartbeat_at=datetime.now())
        )
        return res.rowcount == 1


def reap_leased_tasks(store: StateStore, now: datetime | None = None) -> list[str]:
    """回收租约过期的任务（worker 崩溃恢复）：回 pending 交给其他 worker。"""
    from bdp.db import session_context
    from bdp.models import AgentTask

    now = now or datetime.now()
    with session_context() as s:
        rows = s.execute(
            select(AgentTask).where(
                AgentTask.status == "running",
                AgentTask.lease_expires_at.is_not(None),
                AgentTask.lease_expires_at < now,
            )
        ).scalars().all()
        ids = [t.task_id for t in rows]
        if ids:
            s.execute(
                update(AgentTask)
                .where(AgentTask.task_id.in_(ids))
                .values(status="pending", worker_id="", lease_expires_at=None,
                        error="lease 过期：worker 失联，任务重新入队", retryable=True)
            )
        return ids


class Worker:
    """worker 进程主体：认领 → 执行 → 续租 → 优雅停机。"""

    def __init__(self, worker_id: str, store: StateStore | None = None) -> None:
        self.worker_id = worker_id
        self.store = store or StateStore()
        self.orchestrator = Orchestrator(self.store)
        self._stop = threading.Event()
        self._lease_renewals: dict[str, float] = {}

    def stop(self, *_args) -> None:
        self._stop.set()

    def run_forever(self, poll_interval: float = 1.0) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.store.append_event("", "", "info",
                                f"worker {self.worker_id} 启动（lease={settings.agent_lease_sec}s）")
        while not self._stop.is_set():
            reclaimed = reap_leased_tasks(self.store)
            if reclaimed:
                self.store.append_event("", "", "warn",
                                        f"worker {self.worker_id} 回收 {len(reclaimed)} 个租约过期任务")
            try:
                claimed = claim_leased_tasks(self.store, self.worker_id)
            except Exception as exc:  # DB 抖动不杀 worker
                self.store.append_event("", "", "error",
                                        f"worker {self.worker_id} 认领失败：{exc}")
                claimed = []
            for task in claimed:
                self._run_one(task)
            if not claimed:
                self._stop.wait(poll_interval)
        self.store.append_event("", "", "info", f"worker {self.worker_id} 优雅退出")

    def _run_one(self, task) -> None:
        """执行单个任务：租约续期线程 + 复用 Orchestrator 的任务执行。"""
        dag = self._dag_for(task)
        if dag is None:
            self.store.finish_task(task.task_id, "skipped", {},
                                   f"任务所属 DAG 未知：{task.run_id}", retryable=False)
            return
        renewal = threading.Thread(target=self._renew_loop, args=(task.task_id,), daemon=True)
        with self.orchestrator._meta:
            self.orchestrator._cancel_flags.setdefault(task.task_id, threading.Event())
        renewal.start()
        try:
            self.orchestrator._execute_task(dag, task.run_id, task)
        except Exception as exc:
            self.store.append_event(task.run_id, task.task_id, "error",
                                    f"worker {self.worker_id} 执行异常：{exc}")
            self.store.finish_task(task.task_id, "failed", {}, str(exc), retryable=False)
        finally:
            renewal.join(timeout=2)
            with self.orchestrator._meta:
                self.orchestrator._cancel_flags.pop(task.task_id, None)

    def _renew_loop(self, task_id: str) -> None:
        """租约续期：每 lease/3 秒续一次，直到任务不再是本 worker 的 running。"""
        interval = max(settings.agent_lease_sec / 3, 2)
        while not self._stop.is_set():
            time.sleep(interval)
            try:
                if not renew_lease(self.store, task_id, self.worker_id):
                    return  # 任务已被判死/转派/完成
            except Exception:
                return

    def _dag_for(self, task):
        """任务所属 DAG：DAG 内静态任务查 dag_id；fan-out 子任务回退到父 DAG。"""
        run = self.store.get_run(task.run_id)
        if run is None:
            return None
        dag = DAGS.get(run.dag_id)
        if dag is not None:
            try:
                dag.task(task.name)
                return dag
            except Exception:
                pass
        return dag  # fan-out 子任务：用父 DAG + orchestrator 的 runtime_taskdefs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BDP 独立 worker（process 模式执行体）")
    parser.add_argument("--id", default=f"w-{int(time.time()) % 100000:05d}", help="worker 标识")
    parser.add_argument("--poll", type=float, default=1.0, help="队列轮询间隔（秒）")
    args = parser.parse_args(argv)

    if settings.is_sqlite:
        print("SQLite 是单写者，process 模式不适用；请使用 inline 模式"
              "（当前配置已是 inline，无需启动 worker）或切换 PostgreSQL。", file=sys.stderr)
        return 2

    Worker(args.id).run_forever(poll_interval=args.poll)
    return 0


if __name__ == "__main__":
    sys.exit(main())
