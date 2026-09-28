"""Agent 编排服务 API。

角色边界：
- 触发运行 / 重试是平台级操作（admin/ops），且 ops 必须显式指定租户语境；
- 运行状态查询所有登录角色可见（品牌账号只能看到与自己租户相关的任务细节由
  任务行自身的 tenant_scope 决定，这里不做行级过滤——任务输入/输出不含跨租户数据）。
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from bdp.agents.dag import DAGS, get_dag
from bdp.agents.orchestrator import Orchestrator
from bdp.agents.state import StateStore
from bdp.api.deps import require_roles, tenant_guard
from bdp.config import settings
from bdp.security.auth import Principal

router = APIRouter(tags=["agents"])

# 进程内进行中的编排线程（run_id → Thread）。编排状态在 DB，线程只是执行体。
_running: dict[str, threading.Thread] = {}
_lock = threading.Lock()


class AgentRunRequest(BaseModel):
    dag: str = "nightly"
    params: dict = {}


def _spawn_run(dag_id: str, params: dict, trigger: str) -> str:
    """prepare（入库即返回 run_id）+ 后台线程执行循环——与 CLI 完全同一条编排路径。"""
    dag = get_dag(dag_id)
    orchestrator = Orchestrator()
    run_id = orchestrator.prepare(dag, trigger=trigger, params=params)
    thread = threading.Thread(
        target=orchestrator.run_until_done, args=(dag, run_id),
        daemon=True, name=f"bdp-api-run-{run_id}",
    )
    with _lock:
        _running[run_id] = thread
    thread.start()
    return run_id


@router.post("/v1/agent/runs", summary="触发一次 DAG 运行（后台执行）")
def start_run(
    payload: AgentRunRequest,
    principal: Principal = Depends(require_roles("admin", "ops")),
) -> dict:
    if payload.dag not in DAGS:
        raise HTTPException(status_code=400, detail=f"未知 DAG：{payload.dag}（可用：{sorted(DAGS)}）")
    run_id = _spawn_run(payload.dag, payload.params, trigger=f"api:{principal.username}")
    return {"run_id": run_id, "dag": payload.dag, "status": "running"}


@router.get("/v1/agent/runs", summary="运行列表")
def list_runs(
    _: str | None = Depends(require_roles("admin", "ops", "brand")),
    limit: int = Query(default=20, le=100),
) -> dict:
    runs = StateStore().list_runs(limit=limit)
    return {
        "count": len(runs),
        "items": [
            {
                "run_id": r.run_id, "dag": r.dag_id, "status": r.status,
                "trigger": r.trigger, "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "stats": r.stats,
            }
            for r in runs
        ],
    }


@router.get("/v1/agent/runs/{run_id}", summary="运行明细（任务列表 + artifacts）")
def run_detail(
    run_id: str,
    _: str | None = Depends(require_roles("admin", "ops", "brand")),
) -> dict:
    store = StateStore()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"运行不存在：{run_id}")
    tasks = store.list_tasks(run_id)
    return {
        "run_id": run.run_id, "dag": run.dag_id, "status": run.status,
        "trigger": run.trigger, "params": run.params,
        "started_at": run.started_at.isoformat(), "finished_at": run.finished_at.isoformat()
        if run.finished_at else None,
        "stats": run.stats,
        "artifacts": store.get_artifacts(run_id),
        "tasks": [
            {
                "task_id": t.task_id, "name": t.name, "agent": t.agent,
                "status": t.status, "attempt": t.attempt, "max_attempts": t.max_attempts,
                "tenant": t.tenant_scope, "deps": t.deps,
                "write_domains": t.write_domains, "lock_keys": t.lock_keys,
                "result": t.result, "error": t.error or None,
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "finished_at": t.finished_at.isoformat() if t.finished_at else None,
            }
            for t in tasks
        ],
    }


@router.get("/v1/agent/runs/{run_id}/events", summary="运行事件流（进度与告警）")
def run_events(
    run_id: str,
    _: str | None = Depends(require_roles("admin", "ops", "brand")),
    limit: int = Query(default=100, le=500),
) -> dict:
    events = StateStore().list_events(run_id, limit=limit)
    return {
        "run_id": run_id,
        "events": [
            {
                "ts": e.ts.isoformat(), "task_id": e.task_id, "level": e.level,
                "message": e.message, "data": e.data,
            }
            for e in reversed(events)  # list_events 倒序取，回正时间序
        ],
    }


@router.post("/v1/agent/tasks/{task_id}/retry", summary="复位并重跑失败/跳过的任务及其下游")
def retry_task(
    task_id: str,
    principal: Principal = Depends(require_roles("admin", "ops")),
) -> dict:
    store = StateStore()
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    run = store.get_run(task.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务所属运行不存在")
    dag = get_dag(run.dag_id)
    thread = threading.Thread(
        target=lambda: Orchestrator().resume(dag, run.run_id, [task.task_id]),
        daemon=True, name=f"bdp-api-retry-{task.task_id}",
    )
    thread.start()
    return {"run_id": run.run_id, "task_id": task.task_id, "status": "retrying"}


class AgentAskRequest(BaseModel):
    question: str
    tenant_id: str | None = None  # 仅平台级账号需要显式指定；品牌账号以其绑定租户为准


@router.post("/v1/agent/ask", summary="业务问答（InsightAgent，租户隔离）")
def agent_ask(payload: AgentAskRequest, effective_tenant: str | None = Depends(tenant_guard(
        "agent.ask", "insight_log"))) -> dict:
    """与 /v1/kb/search 相同的租户规则：不接受无租户问答（知识与分析与品牌强绑定）。"""
    from bdp.agents.orchestrator import run_agent_inline
    from bdp.agents.spec import TenantScope

    if not effective_tenant:
        raise HTTPException(
            status_code=400,
            detail="业务问答必须指定租户（X-Tenant-Id），平台级身份不支持跨品牌聚合问答",
        )
    result = run_agent_inline(
        "insight", {"question": payload.question},
        scope=TenantScope(tenant_id=effective_tenant),
        task_name=f"ask:{effective_tenant}",
    )
    insight = result.artifacts.get("insight", {})
    return {
        "question": payload.question,
        "tenant_id": effective_tenant,
        "answer": insight.get("answer", ""),
        "degraded": result.status == "degraded",
        "degraded_reason": result.warning,
        "citations": insight.get("citations", []),
        "mode": result.stats.get("mode"),
    }


@router.get("/v1/agent/capabilities", summary="Agent 能力清单（规格与边界）")
def capabilities(
    _: str | None = Depends(require_roles("admin", "ops", "brand")),
) -> dict:
    from bdp.agents import registry

    registry.ensure_loaded()
    return {
        "workers": Orchestrator.effective_workers(),
        "sqlite_mode": settings.is_sqlite,
        "dq_gate": settings.agent_dq_gate,
        "dq_gate_action": settings.agent_dq_gate_action,
        "dags": {
            dag_id: {
                "description": dag.description,
                "tasks": [
                    {"name": t.name, "agent": t.agent, "deps": t.deps, "fan_out": t.fan_out}
                    for t in dag.tasks
                ],
            }
            for dag_id, dag in DAGS.items()
        },
        "agents": [
            {
                "name": s.name, "description": s.description,
                "write_domains": list(s.write_domains),
                "timeout_sec": s.timeout_sec, "max_attempts": s.max_attempts,
            }
            for s in registry.all_specs()
        ],
    }
