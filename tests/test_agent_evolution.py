"""多 Agent 演进（P0-P3）验收测试。

覆盖：Planner 模板白名单与越权拒绝 / Verifier 只读守卫与复核逻辑 /
worker 租约认领与回收 / 审批闭环（hold→approve/reject→TTL）/ 背压 / 记忆读写与 TTL。
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# P1 Planner：模板白名单 + 越权拒绝
# ---------------------------------------------------------------------------

def test_planner_rule_mapping():
    from bdp.agents.planner import plan_to_dag

    plan, dag = plan_to_dag("分析退款异常波动", "T001")
    templates = [c["template"] for c in plan.to_dict()["template_calls"]]
    assert templates == ["window_metrics", "dq_deep_scan"]
    names = [t.name for t in dag.tasks]
    assert "metrics" in names and "quality" in names
    # 规划产物必须通过现有 DAG 校验（无环/agent 已注册）——plan_to_dag 内已 validate


def test_planner_full_refresh_shape():
    from bdp.agents.planner import plan_to_dag

    _, dag = plan_to_dag("全量刷新数据", None)
    names = [t.name for t in dag.tasks]
    assert names[:4] == ["ingest", "dwd_orders", "dwd_refunds", "dwd_cs"]
    assert "metrics" in names
    # 写域在模板内固化：dwd_orders 只允许写 dwd_order
    dwd = next(t for t in dag.tasks if t.name == "dwd_orders")
    assert dwd.write_domains == ("dwd_order",)


def test_planner_rejects_unknown_and_off():
    from bdp.agents.errors import FatalError
    from bdp.agents.planner import plan_to_dag
    from bdp.config import settings

    with pytest.raises(FatalError):
        plan_to_dag("", None)  # 空目标
    old = settings.agent_planner
    try:
        settings.agent_planner = "off"
        with pytest.raises(FatalError, match="未启用"):
            plan_to_dag("随便分析一下", None)
    finally:
        settings.agent_planner = old


def test_planner_template_schema_hard_validates():
    """模板参数 schema 是安全边界：非法参数在实例化时被拒。"""
    from bdp.agents.planner import TEMPLATE_SCHEMAS
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TEMPLATE_SCHEMAS["window_metrics"].model_validate({"days": "all"})  # 类型错
    with pytest.raises(ValidationError):
        TEMPLATE_SCHEMAS["window_metrics"].model_validate({"days": -5})  # 越界
    with pytest.raises(ValidationError):
        TEMPLATE_SCHEMAS["caliber_diff"].model_validate(
            {"metric_code": "DROP TABLE users"})  # 注入形态被 pattern 拒绝


def test_planner_unknown_template_rejected():
    """未知模板（模拟 LLM 幻觉输出）不能实例化。"""
    from bdp.agents.planner import _taskdefs_for
    from bdp.agents.errors import FatalError

    with pytest.raises(FatalError):
        _taskdefs_for("delete_all_data", None)


# ---------------------------------------------------------------------------
# P2 Verifier：只读守卫 + 复核逻辑
# ---------------------------------------------------------------------------

def test_verifier_is_readonly_by_spec():
    from bdp.agents import registry

    registry.ensure_loaded()
    spec = registry.get("verifier").spec
    assert spec.write_domains == ()


def test_verifier_write_attempt_blocked():
    """Verifier 尝试写库会被 GuardedSession 拦截（WriteDomainViolation）。"""
    import pytest as _pytest

    from bdp.agents.base import guarded_session
    from bdp.agents.errors import WriteDomainViolation
    from bdp.agents import registry

    registry.ensure_loaded()
    spec = registry.get("verifier").spec
    import datetime as _dt

    with _pytest.raises(WriteDomainViolation):
        with guarded_session(spec, commit_on_exit=False) as session:
            from bdp.models import MetricResult

            session.add(MetricResult(
                metric_code="X", caliber_version="v1",
                dt=_dt.date(2026, 1, 1), tenant_id="T001", value=1.0))
            session.flush()  # 守卫在 flush/commit 时拦截（add 本身不触发）


def test_verifier_detects_negative_and_ghost_caliber(seeded):
    from bdp.db import session_scope
    from bdp.agents.orchestrator import run_agent_inline
    from bdp.agents.spec import TenantScope
    from bdp.models import MetricResult

    # 先提交注入（verifier 在独立会话里读，未提交的数据不可见）
    with session_scope() as s:
        s.add(MetricResult(metric_code="FAKE_METRIC",
                           caliber_version="v9", dt=__import__("datetime").date(2026, 9, 1),
                           tenant_id="T001", value=1.0, dim_type="tenant", dim_value="T001"))
        s.add(MetricResult(metric_code="GMV_PAID",
                           caliber_version="v1.1", dt=__import__("datetime").date(2026, 9, 1),
                           tenant_id="T001", value=-999.0, dim_type="tenant", dim_value="T001"))
    result = run_agent_inline("verifier", {"kind": "verify"},
                              scope=TenantScope(tenant_id="T001"))
    verify = result.artifacts["verify"]
    checks = {f["check"]: f["severity"] for f in verify["findings"]}
    assert checks.get("caliber") == "error"
    assert checks.get("negative") == "error"
    assert verify["verdict"] == "fail"
    assert verify["verdict"] == "fail"


# ---------------------------------------------------------------------------
# P0 Worker：租约认领与回收
# ---------------------------------------------------------------------------

def test_lease_claim_respects_pending_and_deps(seeded):
    from bdp.agents.state import StateStore
    from bdp.worker import claim_leased_tasks, reap_leased_tasks

    store = StateStore()
    orchestrator = __import__("bdp.agents.orchestrator", fromlist=["Orchestrator"]).Orchestrator(store)
    from bdp.agents.dag import get_dag

    dag = get_dag("nightly")
    orchestrator.prepare(dag, trigger="test")
    # 认领应只拿到 ingest（其余任务依赖未满足）
    claimed = claim_leased_tasks(store, "w-test", limit=10)
    assert [t.name for t in claimed] == ["ingest"]
    task = claimed[0]
    assert task.worker_id == "w-test"
    assert task.status == "running"
    assert task.lease_expires_at is not None

    # 模拟 worker 失联：租约过期 → 回收回 pending
    reap_leased_tasks(store, __import__("datetime").datetime.max.replace(year=2026))
    task2 = store.get_task(task.task_id)
    assert task2.status == "pending"
    assert task2.worker_id == ""


def test_process_mode_rejected_on_sqlite():
    """SQLite 单写者：process 模式启动 worker 被显式拒绝。"""
    from bdp.config import settings
    from bdp import worker as worker_mod

    assert settings.is_sqlite
    assert worker_mod.main([]) == 2


def test_backpressure_rejects_when_full(seeded):
    from bdp.api.routers.agents import _check_backpressure
    from bdp.config import settings

    old = settings.agent_max_concurrent_runs
    try:
        settings.agent_max_concurrent_runs = 0
        _check_backpressure()  # 0 = 不限
        settings.agent_max_concurrent_runs = 10 ** 9
        _check_backpressure()  # 远未达上限
        settings.agent_max_concurrent_runs = 1
        # 造一个 running run
        from bdp.agents.state import StateStore

        store = StateStore()
        store.create_run("nightly", "test", {})
        with pytest.raises(Exception) as exc_info:
            _check_backpressure()
        assert "并发运行数已达上限" in str(exc_info.value)
    finally:
        settings.agent_max_concurrent_runs = old


# ---------------------------------------------------------------------------
# P2 审批闸口：hold → approve/reject → TTL
# ---------------------------------------------------------------------------

def test_approval_hold_flow(seeded):
    from bdp.agents.state import StateStore
    from bdp.agents.dag import get_dag
    from bdp.agents.orchestrator import Orchestrator

    store = StateStore()
    orch = Orchestrator(store)
    orch.prepare(get_dag("nightly"), trigger="test")
    tasks = store.list_tasks(orch.store.list_runs(limit=1)[0].run_id)
    ingest = next(t for t in tasks if t.name == "ingest")
    store.claim_task(ingest.task_id)  # 置 running（hold_task 只作用于 running）

    assert store.hold_task(ingest.task_id, "门禁阻断待审批")
    held = store.get_task(ingest.task_id)
    assert held.status == "waiting_approval"

    # TTL 未到不回收
    import datetime as dt

    assert store.expire_approvals(dt.datetime.now() - dt.timedelta(minutes=1)) == []
    # approve 后回 pending，可重新执行
    approved = store.approve_task(ingest.task_id, approved_by="admin")
    assert approved is not None
    assert store.get_task(ingest.task_id).status == "pending"
    assert store.get_task(ingest.task_id).approved_by == "admin"


def test_approval_reject_flow(seeded):
    from bdp.agents.state import StateStore
    from bdp.agents.dag import get_dag
    from bdp.agents.orchestrator import Orchestrator

    store = StateStore()
    orch = Orchestrator(store)
    run_id = orch.prepare(get_dag("nightly"), trigger="test")
    ingest = next(t for t in store.list_tasks(run_id) if t.name == "ingest")
    store.claim_task(ingest.task_id)
    store.hold_task(ingest.task_id, "待审批")
    rejected = store.reject_task(ingest.task_id, rejected_by="ops")
    assert rejected is not None
    assert store.get_task(ingest.task_id).status == "skipped"
    # 非等待状态审批返回 None
    assert store.approve_task(ingest.task_id, approved_by="admin") is None


def test_approval_ttl_expiry(seeded):
    import datetime as dt

    from bdp.agents.state import StateStore
    from bdp.agents.dag import get_dag
    from bdp.agents.orchestrator import Orchestrator

    store = StateStore()
    orch = Orchestrator(store)
    run_id = orch.prepare(get_dag("nightly"), trigger="test")
    ingest = next(t for t in store.list_tasks(run_id) if t.name == "ingest")
    store.claim_task(ingest.task_id)
    store.hold_task(ingest.task_id, "待审批")
    # 把 hold 时刻拨回 25 小时前（TTL 默认 24h）
    from bdp.models import AgentTask
    from bdp.db import session_context

    with session_context() as s:
        t = s.get(AgentTask, ingest.task_id)
        t.heartbeat_at = dt.datetime.now() - dt.timedelta(hours=25)
    expired = store.expire_approvals(dt.datetime.now())
    assert ingest.task_id in expired
    assert store.get_task(ingest.task_id).status == "skipped"


# ---------------------------------------------------------------------------
# P3 记忆：读写 / TTL / run 摘要
# ---------------------------------------------------------------------------

def test_memory_roundtrip_and_ttl():

    from bdp.agents import memory

    memory.remember("run_summary", "run-x", "nightly:failed",
                    {"failed_tasks": ["metrics"]}, produced_by="orchestrator")
    val = memory.recall("run_summary", "run-x", "nightly:failed")
    assert val and val["failed_tasks"] == ["metrics"]

    # 过期视同不存在
    memory.remember("run_summary", "run-y", "k", {"v": 1}, ttl_days=-1)
    assert memory.recall("run_summary", "run-y", "k") is None
    memory.prune_expired()


def test_memory_run_summary_skips_green_runs():
    """全绿 run 不沉淀（会稀释记忆）——summarize_run 的设计约定。"""
    from bdp.agents import memory

    memory.summarize_run("run-green", "nightly", "succeeded", {})
    assert memory.recall("run_summary", "run-green", "nightly:succeeded") is None

    memory.summarize_run("run-bad", "nightly", "failed",
                         {"failed_tasks": ["ingest"]}, goal="刷新")
    val = memory.recall("run_summary", "run-bad", "nightly:failed")
    assert val and "ingest" in val["failed_tasks"] and val["goal"] == "刷新"


# ---------------------------------------------------------------------------
# goal API（dry_run 不落库）
# ---------------------------------------------------------------------------

def test_goal_dry_run_no_run_created(client, tokens):
    before = _run_count()
    res = client.post("/v1/agent/goals",
                      headers={"Authorization": f"Bearer {tokens['admin']}"},
                      json={"goal": "分析退款异常", "dry_run": True})
    assert res.status_code == 200
    body = res.json()
    assert body["dry_run"] is True
    assert body["plan"]["planner"] == "rules"
    assert len(body["dag_preview"]) >= 2
    assert _run_count() == before  # 没有创建 run


def test_goal_rejected_for_ops_without_tenant(client, tokens):
    res = client.post("/v1/agent/goals",
                      headers={"Authorization": f"Bearer {tokens['ops']}"},
                      json={"goal": "分析退款异常", "dry_run": True})
    assert res.status_code == 403


def test_goal_forbidden_for_brand(client, tokens):
    res = client.post("/v1/agent/goals",
                      headers={"Authorization": f"Bearer {tokens['nova']}"},
                      json={"goal": "x", "dry_run": True})
    assert res.status_code == 403


def _run_count() -> int:
    from sqlalchemy import func, select

    from bdp.db import session_scope
    from bdp.models import AgentRun

    with session_scope() as s:
        return int(s.execute(select(func.count()).select_from(AgentRun)).scalar_one())
