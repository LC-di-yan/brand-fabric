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
    """按 worker 模式派发：

    - inline（默认）：prepare + 进程内后台线程执行——与 V3 行为一致；
    - process：只 prepare 入队，执行权在独立 worker（python -m bdp.worker）——
      API 重启不再中断编排，多 worker 可水平扩。
    两种模式共享同一条编排状态（DB 任务表），API/CLI 观察方式不变。
    """
    dag = get_dag(dag_id)
    orchestrator = Orchestrator()
    run_id = orchestrator.prepare(dag, trigger=trigger, params=params)
    if settings.agent_worker_mode == "process":
        return run_id  # 入队即返回；worker 进程负责认领执行
    thread = threading.Thread(
        target=orchestrator.run_until_done, args=(dag, run_id),
        daemon=True, name=f"bdp-api-run-{run_id}",
    )
    with _lock:
        _running[run_id] = thread
    thread.start()
    return run_id


def _check_backpressure() -> None:
    """背压：running 状态的 run 数达到上限时拒绝新提交（429）。

    agent_max_concurrent_runs=0 表示不限（测试/单机演示场景）。
    """
    if settings.agent_max_concurrent_runs <= 0:
        return
    running = sum(1 for r in StateStore().list_runs(limit=200) if r.status == "running")
    if running >= settings.agent_max_concurrent_runs:
        raise HTTPException(
            status_code=429,
            detail=f"并发运行数已达上限（{settings.agent_max_concurrent_runs}），"
                   "请等待现有运行结束或调整 BDP_AGENT_MAX_CONCURRENT_RUNS",
        )


@router.post("/v1/agent/runs", summary="触发一次 DAG 运行（后台执行）")
def start_run(
    payload: AgentRunRequest,
    principal: Principal = Depends(require_roles("admin", "ops")),
) -> dict:
    if payload.dag not in DAGS:
        raise HTTPException(status_code=400, detail=f"未知 DAG：{payload.dag}（可用：{sorted(DAGS)}）")
    _check_backpressure()
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
    strategy: str | None = None   # RAG 策略覆盖：single | agentic（缺省读配置）
    thread_id: str | None = None  # 会话 ID（多轮指代消解；缺省 = 新会话）


@router.post("/v1/agent/ask", summary="业务问答（InsightAgent，租户隔离，支持 Agentic RAG）")
def agent_ask(payload: AgentAskRequest, effective_tenant: str | None = Depends(tenant_guard(
        "agent.ask", "insight_log"))) -> dict:
    """与 /v1/kb/search 相同的租户规则：不接受无租户问答（知识与分析与品牌强绑定）。

    strategy="agentic" 启用 Agentic RAG 管线（规划/判定/多跳/压缩/引用核验）；
    thread_id 传入可延续多轮会话（指代消解），缺省新建会话并在响应中返回。
    """
    from bdp.agents.orchestrator import run_agent_inline
    from bdp.agents.spec import TenantScope
    from bdp.rag.session import new_thread_id

    if not effective_tenant:
        raise HTTPException(
            status_code=400,
            detail="业务问答必须指定租户（X-Tenant-Id），平台级身份不支持跨品牌聚合问答",
        )
    if payload.strategy not in (None, "", "single", "agentic"):
        raise HTTPException(status_code=400, detail="strategy 仅支持 single | agentic")
    thread_id = payload.thread_id or new_thread_id()
    result = run_agent_inline(
        "insight",
        {"question": payload.question, "thread_id": thread_id,
         "strategy": payload.strategy or ""},
        scope=TenantScope(tenant_id=effective_tenant),
        task_name=f"ask:{effective_tenant}",
    )
    insight = result.artifacts.get("insight", {})
    return {
        "question": payload.question,
        "tenant_id": effective_tenant,
        "thread_id": insight.get("thread_id") or thread_id,
        "strategy": insight.get("strategy") or "single",
        "confidence": insight.get("confidence", ""),
        "answer": insight.get("answer", ""),
        "degraded": result.status == "degraded",
        "degraded_reason": result.warning,
        "citations": insight.get("citations", []),
        "tool_trace": insight.get("tool_trace", []),
        "mode": result.stats.get("mode"),
    }


@router.get("/v1/rag/trace/{insight_id}", summary="RAG 推理轨迹回放（按问答留痕 ID）")
def rag_trace(
    insight_id: int,
    principal: Principal = Depends(require_roles("admin", "ops", "brand")),
    effective_tenant: str | None = Depends(tenant_guard("rag.trace", "insight_log")),
) -> dict:
    """回放一次问答的管线轨迹（规划/检索/判定/多跳/压缩）。

    brand 账号只能看本租户的留痕；admin/ops 可看全部。
    """
    from sqlalchemy import select

    from bdp.db import get_db
    from bdp.models import InsightLog

    session = next(get_db())
    row = session.execute(
        select(InsightLog).where(InsightLog.id == insight_id)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"留痕 {insight_id} 不存在")
    # brand 角色行级隔离：本租户留痕之外一律 404（不暴露存在性）
    if principal.role == "brand" and row.tenant_id != (principal.tenant_id or ""):
        raise HTTPException(status_code=404, detail=f"留痕 {insight_id} 不存在")

    # 从 tool_trace 里抽出 RAG 管线轨迹（search_kb 的 agentic 调用带完整 steps）
    pipeline_traces = [
        {"query": t.get("args", {}).get("query"), "steps": t.get("trace", [])}
        for t in (row.tool_trace or [])
        if t.get("tool") == "search_kb" and t.get("strategy") == "agentic" and t.get("trace")
    ]
    return {
        "insight_id": row.id,
        "tenant_id": row.tenant_id,
        "thread_id": row.thread_id,
        "question": row.question,
        "degraded": row.degraded,
        "strategy": row.strategy,
        "confidence": row.confidence,
        "elapsed_ms": row.elapsed_ms,
        "tool_trace": row.tool_trace or [],
        "pipeline_traces": pipeline_traces,
    }


class AgentGoalRequest(BaseModel):
    goal: str
    dry_run: bool = False  # true 只返回规划不执行


@router.post("/v1/agent/goals", summary="目标编排：自然语言目标 → 模板白名单 DAG（Planner）")
def agent_goal(
    payload: AgentGoalRequest,
    principal: Principal = Depends(require_roles("admin", "ops")),
    effective_tenant: str | None = Depends(tenant_guard("agent.goal", "agent_run")),
) -> dict:
    """规划与执行分离：dry_run=true 只返回 plan + 预览 DAG，不落任何运行记录。

    租户语义：目标继承发起者身份的租户语境（ops 必须显式指定，规则同 ask）；
    规划器无法跨租户——参数里的租户与身份租户冲突会在 schema 校验层被拒。
    """
    from bdp.agents.planner import plan_to_dag

    if not effective_tenant and principal.role == "ops":
        raise HTTPException(status_code=403, detail="ops 必须显式指定 X-Tenant-Id")
    try:
        plan, dag = plan_to_dag(payload.goal, effective_tenant)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"规划失败：{exc}") from exc

    plan_dict = plan.to_dict()
    preview = [{"name": t.name, "agent": t.agent, "deps": list(t.deps),
                "write_domains": list(t.write_domains or [])} for t in dag.tasks]
    if payload.dry_run:
        return {"dry_run": True, "plan": plan_dict, "dag_preview": preview}

    _check_backpressure()
    # 真实执行：复用 prepare（入队）+ 按模式派发；goal/plan 落 run 表可溯源
    run_id = _spawn_goal(dag, plan_dict, principal.username)
    return {"run_id": run_id, "plan": plan_dict, "dag_preview": preview, "status": "running"}


def _spawn_goal(dag, plan_dict: dict, username: str) -> str:
    """goal 模式与普通 run 的差异只在溯源字段（goal/plan 落 run 表）。"""
    from bdp.db import session_context
    from bdp.models import AgentRun

    orchestrator = Orchestrator()
    run_id = orchestrator.prepare(dag, trigger=f"goal:{username}",
                                  params={"goal": plan_dict["goal"]})
    with session_context() as s:
        run = s.get(AgentRun, run_id)
        if run is not None:
            run.goal = plan_dict["goal"]
            run.plan = plan_dict
    if settings.agent_worker_mode == "process":
        return run_id
    thread = threading.Thread(
        target=orchestrator.run_until_done, args=(dag, run_id),
        daemon=True, name=f"bdp-api-goal-{run_id}",
    )
    with _lock:
        _running[run_id] = thread
    thread.start()
    return run_id


@router.post("/v1/agent/tasks/{task_id}/approve", summary="审批通过 waiting_approval 的任务")
def approve_task(
    task_id: str,
    principal: Principal = Depends(require_roles("admin", "ops")),
) -> dict:

    task = StateStore().approve_task(task_id, approved_by=principal.username)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或不在等待审批状态")
    return {"task_id": task_id, "status": "pending", "approved_by": principal.username}


@router.post("/v1/agent/tasks/{task_id}/reject", summary="审批拒绝 waiting_approval 的任务（下游短路）")
def reject_task(
    task_id: str,
    principal: Principal = Depends(require_roles("admin", "ops")),
) -> dict:
    task = StateStore().reject_task(task_id, rejected_by=principal.username)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或不在等待审批状态")
    return {"task_id": task_id, "status": "skipped", "rejected_by": principal.username}


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
