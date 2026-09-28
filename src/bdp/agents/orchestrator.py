"""Orchestrator：任务分解、派发、并发控制、超时、重试与降级。

运行模型
--------
- 任务状态全部落在 agent_task 表（消息总线），编排循环每轮从库里读真值，
  这样 API/CLI 可以同时观察，进程崩溃后也能凭心跳恢复；
- 派发前用「抢占式领取」（UPDATE ... WHERE status='pending'）防双派发；
- 写域锁按 lock_keys 串行同域任务，异域任务并行；锁在派发时获取、
  由执行线程在 finally 释放——同域任务不会排队占锁；
- 超时实现为协作式取消：到时先置取消标志，任务在批次边界自行退出；
  再超过一倍宽限期则强制判死（执行线程持有的锁等线程自然退出后释放）；
- SQLite 是单写者，有效并发强制为 1（诚实面对约束，而不是假装能并行）。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from bdp.agents import registry
from bdp.agents.base import guarded_session
from bdp.agents.dag import Dag, TaskDef
from bdp.agents.errors import FatalError, RetryableError, TaskCancelled
from bdp.agents.spec import AgentContext, AgentResult, TenantScope
from bdp.agents.state import TERMINAL_STATUSES, StateStore
from bdp.config import settings

_SUCCESSISH = ("succeeded", "degraded")
_FAILUREISH = ("failed", "skipped", "cancelled")


class Orchestrator:
    def __init__(self, store: StateStore | None = None) -> None:
        self.store = store or StateStore()
        self._domain_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._cancel_flags: dict[str, threading.Event] = {}
        self._ready_at: dict[str, float] = {}
        self._inflight: set[str] = set()
        self._fanout_spawned: set[str] = set()
        self._groups_finalized: set[str] = set()
        # fan-out 子任务的运行时 TaskDef（DAG 里不声明，派发时需要）
        self._runtime_taskdefs: dict[tuple[str, str], TaskDef] = {}
        self._meta = threading.Lock()

    # ------------------------------------------------------------------ 公共入口

    def execute(self, dag: Dag, *, trigger: str = "cli", params: dict | None = None) -> str:
        """同步执行一个 DAG，返回 run_id。CLI 使用。"""
        run_id = self.prepare(dag, trigger=trigger, params=params)
        self.run_until_done(dag, run_id)
        return run_id

    def prepare(self, dag: Dag, *, trigger: str = "cli", params: dict | None = None) -> str:
        """创建 run 并入队全部任务（不执行）。API 异步运行的第一步。"""
        registry.ensure_loaded()
        dag.validate()
        params = params or {}

        self.store.reap_stale_tasks(datetime.now() - self._reap_threshold(dag))
        run_id = self.store.create_run(dag.dag_id, trigger, params)
        workers = self.effective_workers()
        self.store.append_event(
            run_id, "", "info", f"运行开始：{dag.dag_id}（workers={workers}）", {"params": params}
        )

        for td in dag.tasks:
            self._enqueue(dag, td, run_id, params)
        return run_id

    def run_until_done(self, dag: Dag, run_id: str) -> None:
        """执行循环直到 run 内全部任务终态并汇总（API 后台线程与 CLI 共用）。"""
        workers = self.effective_workers()
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bdp-agent") as pool:
            self._loop(dag, run_id, pool)

    def resume(self, dag: Dag, run_id: str, task_ids: list[str]) -> str:
        """把指定任务（及其传递依赖的失败任务）复位后继续跑同一个 run。"""
        registry.ensure_loaded()
        dag.validate()
        self._reset_for_retry(run_id, task_ids)
        with ThreadPoolExecutor(
            max_workers=self.effective_workers(), thread_name_prefix="bdp-agent"
        ) as pool:
            self._loop(dag, run_id, pool)
        return run_id

    def _reset_for_retry(self, run_id: str, task_ids: list[str]) -> None:
        retried: set[str] = set()
        for task_id in task_ids:
            task = self.store.get_task(task_id)
            if task is None:
                raise FatalError(f"任务不存在：{task_id}")
            if self.store.retry_failed_task(task_id):
                retried.add(task.name)
                self.store.append_event(run_id, task_id, "info", f"人工重试：复位任务 {task.name}")
        # 传递复位：失败链路上的下游任务一并回到 pending
        changed = True
        while changed and retried:
            changed = False
            for t in self.store.list_tasks(run_id):
                if t.status in _FAILUREISH and t.name not in retried:
                    if any(d in retried for d in (t.deps or [])) or (t.group and t.group in retried):
                        self.store.retry_failed_task(t.task_id)
                        retried.add(t.name)
                        changed = True

    @staticmethod
    def effective_workers() -> int:
        # SQLite 单写者：并发只会带来锁等待与混乱，诚实串行
        return 1 if settings.is_sqlite else max(1, settings.agent_workers)

    # ------------------------------------------------------------------ 主循环

    def _loop(self, dag: Dag, run_id: str, pool: ThreadPoolExecutor) -> None:
        while True:
            tasks = self.store.list_tasks(run_id)
            self._propagate_skips(dag, tasks)
            self._check_timeouts(tasks)
            if self._spawn_fanout_if_ready(dag, tasks, run_id):
                continue
            self._finalize_groups(tasks, run_id)
            tasks = self.store.list_tasks(run_id)
            if all(t.status in TERMINAL_STATUSES for t in tasks):
                break
            submitted = self._dispatch_ready(dag, run_id, tasks, pool)
            time.sleep(0.02 if submitted else 0.1)

        self._summarize_run(dag, run_id)

    def _dispatch_ready(self, dag: Dag, run_id: str, tasks, pool: ThreadPoolExecutor) -> int:
        """把依赖就绪、锁空闲、退避到期的任务提交执行。返回本轮提交数。"""
        submitted = 0
        free_slots = max(0, self.effective_workers() - len(self._inflight))
        now = time.time()
        for task in tasks:
            if submitted >= free_slots:
                break
            if task.status != "pending":
                continue
            if self._ready_at.get(task.task_id, 0) > now:
                continue
            state = self._dep_state(task, tasks)
            if state == "blocked":
                continue  # 由 _propagate_skips 处理
            if state == "waiting":
                continue
            td = self._taskdef_for(dag, task)
            locks = self._lock_keys_for(td, task)
            acquired = self._acquire_locks(locks)
            if not acquired:
                continue
            ok = self.store.claim_task(task.task_id)
            if not ok:
                self._release_locks(locks)
                continue
            with self._meta:
                self._inflight.add(task.task_id)
                self._cancel_flags.setdefault(task.task_id, threading.Event())
            pool.submit(self._runner, dag, run_id, task, locks)
            submitted += 1
        return submitted

    def _runner(self, dag: Dag, run_id: str, task, locks: list[str]) -> None:
        try:
            self._execute_task(dag, run_id, task)
        except Exception as exc:  # 兜底：runner 崩了不能拖垮编排循环
            self.store.append_event(run_id, task.task_id, "error", f"执行线程异常：{exc}")
            self.store.finish_task(task.task_id, "failed", {}, str(exc), retryable=False)
        finally:
            self._release_locks(locks)
            with self._meta:
                self._inflight.discard(task.task_id)
                self._cancel_flags.pop(task.task_id, None)

    def _execute_task(self, dag: Dag, run_id: str, task) -> None:
        td = self._taskdef_for(dag, task)
        agent = registry.get(task.agent)
        spec = agent.spec

        artifacts = {
            k: v
            for k, v in self.store.get_artifacts(run_id).items()
            if k in (td.artifact_keys or [])
        }
        window = self._window_of(artifacts)
        input_payload = {"params": task.params or {}, "artifacts": artifacts}
        self.store.update_input(task.task_id, input_payload)

        def emit(level: str = "info", message: str = "", data: dict | None = None) -> None:
            self.store.append_event(run_id, task.task_id, level, message, data)
            self.store.touch_heartbeat(task.task_id)

        ctx = AgentContext(
            run_id=run_id,
            task_id=task.task_id,
            task_name=task.name,
            scope=TenantScope(tenant_id=task.tenant_scope),
            params=task.params or {},
            artifacts=artifacts,
            window=window,
            session_factory=lambda commit_on_exit=True: guarded_session(
                spec, domains=td.write_domains, commit_on_exit=commit_on_exit
            ),
            cancel=lambda: self._cancel_flags.get(task.task_id, threading.Event()).is_set(),
            emit=emit,
        )

        started = time.monotonic()
        try:
            result = agent.run(ctx)
        except (TaskCancelled, RetryableError) as exc:
            self._handle_failure(dag, run_id, task, exc, True, started)
            return
        except Exception as exc:  # 未知/致命异常一律不重试（重试风暴保护）
            self._handle_failure(dag, run_id, task, exc, False, started)
            return

        self._handle_result(dag, run_id, task, td, result)

    # ------------------------------------------------------------------ 结果与失败

    def _handle_result(self, dag: Dag, run_id: str, task, td: TaskDef, result: AgentResult) -> None:
        status = result.status if result.status in (*_SUCCESSISH, "skipped") else "succeeded"
        # 顺序很关键：先写 artifact、展开 fan-out 子任务，最后才标记本任务完成——
        # 否则主循环会在"父任务已终态、子任务尚未入库"的窗口里误判运行结束
        for key, value in (result.artifacts or {}).items():
            self.store.put_artifact(run_id, key, value, task.name)
        if td.fan_out == "tenants" and status in _SUCCESSISH:
            self._spawn_tenant_children(dag, run_id, task)
        self.store.finish_task(
            task.task_id, status,
            {"status": status, "artifacts": result.artifacts, "stats": result.stats,
             "warning": result.warning},
        )
        if result.warning:
            self.store.append_event(run_id, task.task_id, "warn", result.warning)
        self.store.append_event(
            run_id, task.task_id, "info", f"任务完成：{status}",
            {"stats": result.stats, "artifacts": list((result.artifacts or {}).keys())},
        )

    def _handle_failure(self, dag: Dag, run_id: str, task, exc: Exception,
                        retryable: bool, started: float) -> None:
        attempt = task.attempt
        exhausted = attempt >= task.max_attempts
        if retryable and not exhausted:
            delay = self._backoff_delay(attempt)
            with self._meta:
                self._ready_at[task.task_id] = time.time() + delay
            self.store.requeue_task(task.task_id, f"第 {attempt} 次失败（可重试）：{exc}")
            self.store.append_event(
                run_id, task.task_id, "warn",
                f"失败待重试（attempt {attempt}/{task.max_attempts}，退避 {delay}s）：{exc}",
            )
            return

        self.store.finish_task(
            task.task_id, "failed", {}, f"{type(exc).__name__}: {exc}", retryable=retryable
        )
        self.store.append_event(
            run_id, task.task_id, "error",
            f"任务失败（重试{'已' if exhausted else '不'}适用）：{exc}",
        )

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        """指数退避：5s × 2^(n-1)，封顶 60s。测试可覆盖以加速。"""
        return min(5 * (2 ** max(0, attempt - 1)), 60)

    # ------------------------------------------------------------------ fan-out / 分组

    def _spawn_fanout_if_ready(self, dag: Dag, tasks, run_id: str) -> bool:
        """fan-out 父任务成功后展开租户子任务。返回是否发生了变更。"""
        changed = False
        for task in tasks:
            if task.status not in _SUCCESSISH:
                continue
            td = self._taskdef_for(dag, task)
            if td.fan_out != "tenants" or task.name in self._fanout_spawned:
                continue
            self._spawn_tenant_children(dag, run_id, task)
            changed = True
        return changed

    def _spawn_tenant_children(self, dag: Dag, run_id: str, parent) -> None:
        with self._meta:
            if parent.name in self._fanout_spawned:
                return
            self._fanout_spawned.add(parent.name)

        # resume 安全：该父任务的子任务若已存在（上次运行展开过），不再重复展开
        existing = [t for t in self.store.list_tasks(run_id) if t.group == parent.name]
        if existing:
            return

        tenant_ids = self._tenant_ids()
        if not tenant_ids:
            self.store.append_event(run_id, parent.task_id, "warn", "fan-out 无租户可展开")
            return
        for tenant_id in tenant_ids:
            td = TaskDef(
                name=f"{parent.name}#{tenant_id}",
                agent=parent.agent,
                deps=[parent.name],
                params={"kind": "materialize_tenant"},
                artifact_keys=["metrics"],
                write_domains=("metric_result",),
                lock_keys=[f"metric:{tenant_id}"],
            )
            self._runtime_taskdefs[(run_id, td.name)] = td
            self.store.enqueue_task(
                run_id, name=td.name, agent=td.agent,
                spec=registry.get(td.agent).spec,
                deps=td.deps, params=td.params, input_payload={},
                tenant_scope=tenant_id, lock_keys=td.lock_keys,
                parent_task=parent.name, group=parent.name,
            )
        self.store.append_event(
            run_id, parent.task_id, "info",
            f"fan-out 展开 {len(tenant_ids)} 个租户子任务",
            {"tenants": tenant_ids},
        )

    def _finalize_groups(self, tasks, run_id: str) -> None:
        groups: dict[str, list] = {}
        for t in tasks:
            if t.group:
                groups.setdefault(t.group, []).append(t)
        for group, members in groups.items():
            if group in self._groups_finalized:
                continue
            if not all(t.status in TERMINAL_STATUSES for t in members):
                continue
            per_tenant = {
                t.tenant_scope: {"status": t.status, "written": (t.result or {}).get("stats", {}).get("written")}
                for t in members
            }
            failed = [t.tenant_scope for t in members if t.status not in _SUCCESSISH]
            self.store.put_artifact(
                run_id, "materialize",
                {"tenants": per_tenant, "failed_tenants": failed,
                 "total_written": sum(int(v.get("written") or 0) for v in per_tenant.values())},
                producer_task=group,
            )
            self._groups_finalized.add(group)
            self.store.append_event(
                run_id, "", "info", f"fan-in 完成：{group}",
                {"failed_tenants": failed},
            )

    # ------------------------------------------------------------------ 依赖 / 跳过 / 超时

    def _dep_state(self, task, tasks) -> str:
        """waiting | ready | blocked。fan-out 组对组外任务是一个整体依赖，
        组内子任务只依赖父任务本身（否则子任务会互相等待造成死锁）。"""
        if not task.deps:
            return "ready"
        for dep in task.deps:
            relevant = [t for t in tasks if t.name == dep or t.group == dep]
            if task.group:
                relevant = [t for t in relevant if t.group != task.group]
            if not relevant:
                return "waiting"
            statuses = [t.status for t in relevant]
            if any(s in _FAILUREISH for s in statuses):
                return "blocked"
            if all(s in TERMINAL_STATUSES for s in statuses):
                continue
            return "waiting"
        return "ready"

    def _propagate_skips(self, dag: Dag, tasks) -> None:
        for task in tasks:
            if task.status != "pending":
                continue
            blocked_by = next(
                (dep for dep in task.deps if self._dep_blocked_by(dep, tasks, task)), None
            )
            if blocked_by:
                self.store.mark_skipped(task.task_id, f"上游任务 {blocked_by} 未成功，短路跳过")
                self.store.append_event(
                    task.run_id, task.task_id, "warn", f"上游 {blocked_by} 未成功，本任务跳过"
                )

    @staticmethod
    def _dep_blocked_by(dep: str, tasks, task) -> bool:
        relevant = [t for t in tasks if t.name == dep or t.group == dep]
        if task.group:
            relevant = [t for t in relevant if t.group != task.group]
        return any(t.status in _FAILUREISH for t in relevant)

    def _check_timeouts(self, tasks) -> None:
        now = datetime.now()
        for task in tasks:
            if task.status != "running" or not task.started_at:
                continue
            elapsed = (now - task.started_at).total_seconds()
            flag = self._cancel_flags.get(task.task_id)
            if elapsed > task.timeout_sec and flag and not flag.is_set():
                flag.set()
                self.store.append_event(
                    task.run_id, task.task_id, "warn",
                    f"任务超时（>{task.timeout_sec}s），已发送协作取消信号",
                )
            grace = max(task.timeout_sec, 60)
            if elapsed > task.timeout_sec + grace:
                # 强制判死；执行线程持有的写域锁等线程自然退出后释放，
                # finish 的 status 守卫保证线程稍后完成时不会覆盖该判定
                self.store.finish_task(
                    task.task_id, "failed", {}, f"timeout: 超过 {task.timeout_sec}s + {grace}s 宽限",
                    retryable=True,
                )
                self.store.append_event(task.run_id, task.task_id, "error", "任务超时判死")

    # ------------------------------------------------------------------ 杂项

    def _enqueue(self, dag: Dag, td: TaskDef, run_id: str, params: dict) -> str:
        task_params = dict(td.params)
        if td.name == "ingest" and params:
            task_params.update({k: v for k, v in params.items() if k in ("days", "seed", "replace")})
        return self.store.enqueue_task(
            run_id, name=td.name, agent=td.agent,
            spec=registry.get(td.agent).spec,
            deps=list(td.deps), params=task_params, input_payload={},
            tenant_scope=None, lock_keys=self._lock_keys_for(td, None),
        )

    def _taskdef_for(self, dag: Dag, task) -> TaskDef:
        try:
            return dag.task(task.name)
        except FatalError:
            td = self._runtime_taskdefs.get((task.run_id, task.name))
            if td is None:
                raise
            return td

    def _lock_keys_for(self, td: TaskDef, task) -> list[str]:
        if td.lock_keys:
            return list(td.lock_keys)
        return list(td.write_domains or registry.get(td.agent).spec.write_domains)

    def _acquire_locks(self, keys: list[str]) -> bool:
        with self._locks_guard:
            for key in sorted(set(keys)):
                lock = self._domain_locks.setdefault(key, threading.Lock())
                if not lock.acquire(blocking=False):
                    for k in sorted(set(keys)):
                        if k == key:
                            break
                        self._domain_locks[k].release()
                    return False
            return True

    def _release_locks(self, keys: list[str]) -> None:
        with self._locks_guard:
            for key in sorted(set(keys)):
                lock = self._domain_locks.get(key)
                if lock is not None:
                    try:
                        lock.release()
                    except RuntimeError:
                        pass

    def _tenant_ids(self) -> list[str]:
        from sqlalchemy import text

        from bdp.db import session_context

        with session_context(commit_on_exit=False) as s:
            return [r[0] for r in s.execute(text("SELECT tenant_id FROM dim_tenant ORDER BY tenant_id")).all()]

    @staticmethod
    def _window_of(artifacts: dict) -> tuple[date, date] | None:
        """从黑板提取数据窗口：优先 ingest（生成窗口），否则 metrics（物化窗口）。"""
        for key in ("ingest", "metrics"):
            payload = artifacts.get(key) or {}
            window = payload.get("window") or (
                {"start": payload.get("start"), "end": payload.get("end")}
                if payload
                else None
            )
            if window and window.get("start") and window.get("end"):
                try:
                    return date.fromisoformat(str(window["start"])), date.fromisoformat(str(window["end"]))
                except ValueError:
                    continue
        return None

    @staticmethod
    def _reap_threshold(dag: Dag) -> timedelta:
        registry.ensure_loaded()
        timeouts = [registry.get(t.agent).spec.timeout_sec for t in dag.tasks]
        max_timeout = max(timeouts, default=900)
        return timedelta(seconds=max(600, 2 * max_timeout))

    def _summarize_run(self, dag: Dag, run_id: str) -> None:
        tasks = self.store.list_tasks(run_id)
        statuses = {t.name: t.status for t in tasks}
        failed = [n for n, s in statuses.items() if s == "failed"]
        skipped = [n for n, s in statuses.items() if s in ("skipped", "cancelled")]
        degraded = [n for n, s in statuses.items() if s == "degraded"]
        if failed:
            status = "failed"
        elif skipped:
            status = "partial_success"
        else:
            status = "succeeded"
        stats = {
            "tasks": statuses,
            "failed_tasks": failed,
            "skipped_tasks": skipped,
            "degraded_tasks": degraded,
            "artifacts": sorted(self.store.get_artifacts(run_id).keys()),
        }
        self.store.finish_run(run_id, status, stats)
        self.store.append_event(run_id, "", "info" if status == "succeeded" else "warn",
                                f"运行结束：{status}")
        # 事件按保留期清理（每次运行顺带执行）
        self.store.prune_events(datetime.now() - timedelta(days=settings.agent_event_retention_days))


def run_agent_inline(
    agent_name: str,
    params: dict | None = None,
    *,
    scope: TenantScope | None = None,
    artifacts: dict | None = None,
    task_name: str | None = None,
) -> AgentResult:
    """不走队列、同步执行单个 agent——旧 CLI 命令与交互式 API 共用的入口。"""
    registry.ensure_loaded()
    agent = registry.get(agent_name)
    ctx = AgentContext(
        run_id="inline", task_id="inline",
        task_name=task_name or f"inline:{agent_name}",
        scope=scope or TenantScope.platform(),
        params=params or {}, artifacts=artifacts or {}, window=None,
        session_factory=lambda commit_on_exit=True: guarded_session(
            agent.spec, commit_on_exit=commit_on_exit
        ),
        cancel=lambda: False,
        emit=lambda *args, **kwargs: None,
    )
    return agent.run(ctx)
