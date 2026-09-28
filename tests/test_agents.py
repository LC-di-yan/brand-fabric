"""多 agent 系统集成测试。

覆盖方案验收标准：
- 写域守卫（三种写入路径全部拦截）
- DAG 校验（环 / 缺依赖 / 缺 agent）
- nightly DAG 与旧路径（conftest seeded 直调）结果等价
- fan-out/fan-in（按租户子任务 + materialize 黑板汇总）
- 失败短路（上游失败 → 下游 skipped → run=failed）
- 可重试错误退避重试后成功
- DQ 门禁（gate 收紧 → skip 短路 / degraded 继续两种动作）
- 协作式取消（超时 → 任务在批次边界退出）
- 心跳超时崩溃恢复
- inline 直调入口

注意：nightly 等价性测试会重跑全链路（幂等），放在文件最后执行。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest
from pydantic import BaseModel
from sqlalchemy import text

from .conftest import auth

from bdp.agents.dag import Dag, TaskDef
from bdp.agents.errors import RetryableError, WriteDomainViolation
from bdp.agents.orchestrator import Orchestrator, run_agent_inline
from bdp.agents.spec import AgentSpec, TenantScope
from bdp.agents.state import StateStore
from bdp.config import settings


# ---------------------------------------------------------------------------
# 写域守卫
# ---------------------------------------------------------------------------


def _quality_spec() -> AgentSpec:
    return AgentSpec(name="guard_probe", write_domains=("dq",), timeout_sec=10, max_attempts=1)


def test_guard_blocks_session_add(seeded):
    from bdp.agents.base import guarded_session
    from bdp.models import DwdOrder

    with pytest.raises(WriteDomainViolation):
        with guarded_session(_quality_spec()) as session:
            session.add(DwdOrder(order_line_id="X", tenant_id="T001", shop_id="S",
                                 platform="tmall", order_id="O", qty=1, pay_amount=1.0,
                                 discount=0, freight=0, net_amount=1, order_status="paid",
                                 order_dt=datetime.now().date()))
            session.flush()


def test_guard_blocks_orm_delete(seeded):
    from bdp.agents.base import guarded_session
    from bdp.models import DwdOrder
    from sqlalchemy import delete

    with pytest.raises(WriteDomainViolation):
        with guarded_session(_quality_spec()) as session:
            session.execute(delete(DwdOrder))


def test_guard_blocks_bulk_insert(seeded):
    from bdp.agents.base import guarded_session
    from bdp.models import DwdOrder

    with pytest.raises(WriteDomainViolation):
        with guarded_session(_quality_spec()) as session:
            session.bulk_insert_mappings(DwdOrder, [{
                "order_line_id": "X", "tenant_id": "T001", "shop_id": "S",
                "platform": "tmall", "order_id": "O", "qty": 1, "pay_amount": 1.0,
                "discount": 0, "freight": 0, "net_amount": 1, "order_status": "paid",
                "order_dt": datetime.now().date(),
            }])


def test_guard_allows_declared_domain(seeded):
    from sqlalchemy import delete

    from bdp.agents.base import guarded_session
    from bdp.db import session_scope
    from bdp.models import DqRule

    with guarded_session(_quality_spec()) as session:
        session.add(DqRule(rule_id="GUARD-PROBE", table_name="raw_order", rule_type="not_null",
                           column_name="x", expression="1=1", severity="warn", description="probe"))
    # with 块正常退出即提交成功，未触发守卫；清理探针数据
    with session_scope() as session:
        session.execute(delete(DqRule).where(DqRule.rule_id == "GUARD-PROBE"))


# ---------------------------------------------------------------------------
# DAG 校验
# ---------------------------------------------------------------------------


def test_dag_rejects_cycle():
    dag = Dag("bad", tasks=[
        TaskDef(name="a", agent="kb", deps=["b"]),
        TaskDef(name="b", agent="kb", deps=["a"]),
    ])
    with pytest.raises(Exception, match="循环依赖"):
        dag.validate()


def test_dag_rejects_unknown_agent():
    dag = Dag("bad2", tasks=[TaskDef(name="a", agent="no_such_agent")])
    with pytest.raises(Exception, match="未注册"):
        dag.validate()


def test_dag_rejects_missing_dep():
    dag = Dag("bad3", tasks=[TaskDef(name="a", agent="kb", deps=["ghost"])])
    with pytest.raises(Exception, match="不存在"):
        dag.validate()


# ---------------------------------------------------------------------------
# inline 直调
# ---------------------------------------------------------------------------


def test_run_agent_inline_quality(seeded):
    result = run_agent_inline("quality", {})
    assert result.status == "succeeded"
    assert result.artifacts["dq"]["run_id"]
    assert 0 < result.artifacts["dq"]["pass_rate"] <= 1


# ---------------------------------------------------------------------------
# 失败短路 / 重试 / 门禁 / 取消
# ---------------------------------------------------------------------------


def _restore_fixture_state():
    """恢复共享夹具状态：重新注入 DQ-INJ 脏行并按旧路径重算。

    （后续测试如 test_enterprise_api::test_data_quality_samples 依赖这些样本）
    """
    from datetime import date as _date

    from bdp.db import session_scope
    from bdp.metrics.engine import materialize
    from bdp.pipeline.dwd import build_dwd
    from bdp.pipeline.dws import build_dws
    from bdp.pipeline.quality import run_quality_checks
    from sqlalchemy import text as _text

    from tests.conftest import inject_dq_violations

    inject_dq_violations()
    with session_scope() as session:
        build_dwd(session)
    with session_scope() as session:
        build_dws(session)
        run_quality_checks(session)
    with session_scope() as session:
        start = session.execute(_text("SELECT MIN(dt) FROM dws_tenant_day")).scalar()
        end = session.execute(_text("SELECT MAX(dt) FROM dws_tenant_day")).scalar()
        materialize(session, start=_date.fromisoformat(str(start)), end=_date.fromisoformat(str(end)))


def _make_orchestrator() -> Orchestrator:
    orch = Orchestrator()
    orch._backoff_delay = staticmethod(lambda attempt: 0.05)  # 测试加速
    return orch


def test_fatal_failure_skips_downstream(seeded, monkeypatch):
    monkeypatch.setattr(
        "bdp.kb.ingest.ingest_knowledge",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("embedding 后端配置错误")),
    )
    dag = Dag("test-skip", tasks=[
        TaskDef(name="kb_boom", agent="kb"),
        TaskDef(name="after", agent="pipeline", deps=["kb_boom"], params={"domain": "dws"}),
    ])
    run_id = _make_orchestrator().execute(dag, trigger="test")
    tasks = {t.name: t.status for t in StateStore().list_tasks(run_id)}
    assert tasks["kb_boom"] == "failed"
    assert tasks["after"] == "skipped"
    run = StateStore().get_run(run_id)
    assert run.status == "failed"


def test_retryable_error_is_retried_then_succeeds(seeded, monkeypatch):
    from bdp.kb.ingest import ingest_knowledge as original

    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetryableError("embedding 服务暂时不可用")
        return original(*args, **kwargs)

    monkeypatch.setattr("bdp.kb.ingest.ingest_knowledge", flaky)

    dag = Dag("test-retry", tasks=[TaskDef(name="kb_retry", agent="kb")])
    run_id = _make_orchestrator().execute(dag, trigger="test")
    store = StateStore()
    task = next(t for t in store.list_tasks(run_id) if t.name == "kb_retry")
    assert task.status == "succeeded"
    assert task.attempt == 2
    assert calls["n"] == 2


def test_dq_gate_skip_and_degraded(seeded, monkeypatch):
    monkeypatch.setattr(settings, "agent_dq_gate", 1.0)  # 演示数据必然有脏行 → verdict=fail

    # 动作=skip：metrics 准备任务被跳过，不展开子任务
    monkeypatch.setattr(settings, "agent_dq_gate_action", "skip")
    dag = Dag("test-gate-skip", tasks=[
        TaskDef(name="quality", agent="quality"),
        TaskDef(name="metrics", agent="metrics", deps=["quality"],
                params={"kind": "prepare"}, artifact_keys=["dq"], fan_out="tenants"),
    ])
    run_id = _make_orchestrator().execute(dag, trigger="test")
    tasks = {t.name: t.status for t in StateStore().list_tasks(run_id)}
    assert tasks["quality"] == "succeeded"
    assert tasks["metrics"] == "skipped"
    assert not any(t.name.startswith("metrics#") for t in StateStore().list_tasks(run_id))
    assert StateStore().get_run(run_id).status == "partial_success"

    # 动作=degraded：带标记继续物化（子任务以 degraded 完成）
    monkeypatch.setattr(settings, "agent_dq_gate_action", "degraded")
    run_id = _make_orchestrator().execute(dag, trigger="test")
    store = StateStore()
    tasks = {t.name: t.status for t in store.list_tasks(run_id)}
    assert tasks["metrics"] == "degraded"
    children = [s for name, s in tasks.items() if name.startswith("metrics#")]
    assert children and all(s == "degraded" for s in children)
    assert store.get_artifacts(run_id)["metrics"]["gate"] == "degraded"


def test_cooperative_cancel_on_timeout(seeded):
    class SlowInput(BaseModel):
        pass

    from bdp.agents import registry
    from bdp.agents.spec import AgentContext, AgentResult

    class SlowAgent:
        spec = AgentSpec(name="slow_probe", write_domains=(), timeout_sec=1, max_attempts=1)

        def run(self, ctx: AgentContext) -> AgentResult:
            for _ in range(200):
                ctx.check_cancel()
                time.sleep(0.05)
            return AgentResult("succeeded")

    registry.register(SlowAgent())
    try:
        dag = Dag("test-cancel", tasks=[TaskDef(name="slow", agent="slow_probe")])
        run_id = _make_orchestrator().execute(dag, trigger="test")
        store = StateStore()
        task = next(t for t in store.list_tasks(run_id) if t.name == "slow")
        assert task.status == "failed"
        assert task.retryable is True
        assert "取消" in task.error or "timeout" in task.error or "超时" in task.error
    finally:
        registry._REGISTRY.pop("slow_probe", None)


def test_stale_heartbeat_is_reaped(seeded):
    from bdp.agents.kb_agent import KbAgent
    from bdp.db import session_context
    from bdp.models import AgentTask
    from sqlalchemy import update

    store = StateStore()
    run_id = store.create_run("test-reap", "test", {})
    task_id = store.enqueue_task(
        run_id, name="stale", agent="kb", spec=KbAgent.spec, deps=[],
        params={}, input_payload={}, tenant_scope=None, lock_keys=["kb_chunk"],
    )
    assert store.claim_task(task_id)
    stale = datetime.now() - timedelta(hours=2)
    with session_context() as s:
        s.execute(update(AgentTask).where(AgentTask.task_id == task_id)
                  .values(heartbeat_at=stale))

    reaped = store.reap_stale_tasks(datetime.now() - timedelta(seconds=600))
    assert task_id in reaped
    task = store.get_task(task_id)
    assert task.status == "failed" and task.retryable is True


# ---------------------------------------------------------------------------
# nightly DAG：等价性 + fan-out（重改动放最后）
# ---------------------------------------------------------------------------


def _snapshot(session) -> dict:
    tables = [
        "dim_tenant", "dim_shop", "dim_spu", "dim_sku", "map_platform_sku",
        "raw_order", "raw_refund", "raw_cs_session",
        "dwd_order", "dwd_refund", "dwd_cs_session",
        "dws_shop_day", "dws_tenant_day", "metric_def", "metric_result",
        "kb_document", "kb_chunk",
    ]
    counts = {
        t: int(session.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() or 0)
        for t in tables
    }
    sums = {
        "metric_result_sum": session.execute(
            text("SELECT COALESCE(ROUND(SUM(value), 4), 0) FROM metric_result")
        ).scalar(),
        "dws_paid_gmv_sum": session.execute(
            text("SELECT COALESCE(ROUND(SUM(paid_gmv), 2), 0) FROM dws_tenant_day")
        ).scalar(),
    }
    dq = session.execute(
        text(
            "SELECT rule_id, failed_rows, pass_rate FROM dq_result "
            "WHERE run_id = (SELECT MAX(run_id) FROM dq_result) ORDER BY rule_id"
        )
    ).all()
    return {"counts": counts, "sums": sums, "dq": [tuple(r) for r in dq]}


def test_nightly_dag_equivalent_to_legacy_and_idempotent(seeded):
    """conftest seeded 夹具代表旧路径（cli all）的产物；nightly DAG 重跑同参数
    全链路后，业务结果必须逐项等价；再跑一次仍等价（幂等）。

    口径对齐：seeded 夹具在生成后注入了 4 条 DQ-INJ 测试脏行（让质量断言稳定），
    agent 的 mock 接入是确定性再生、不含这些注入。先剔除注入行并按旧路径重算，
    使 before 成为与 agent 接入同源的旧路径产物。
    """
    from datetime import date as _date

    from bdp.db import session_scope
    from bdp.metrics.engine import materialize
    from bdp.models import RawOrder, RawRefund
    from bdp.pipeline.dwd import build_dwd
    from bdp.pipeline.dws import build_dws
    from bdp.pipeline.quality import run_quality_checks

    with session_scope() as session:
        session.query(RawOrder).filter(
            RawOrder.order_line_id.like("DQ-INJ-%")
        ).delete(synchronize_session=False)
        session.query(RawRefund).filter(
            RawRefund.refund_id == "DQ-INJ-R1"
        ).delete(synchronize_session=False)
    with session_scope() as session:
        build_dwd(session)
    with session_scope() as session:
        build_dws(session)
        run_quality_checks(session)
    with session_scope() as session:
        start = session.execute(text("SELECT MIN(dt) FROM dws_tenant_day")).scalar()
        end = session.execute(text("SELECT MAX(dt) FROM dws_tenant_day")).scalar()
        materialize(session, start=_date.fromisoformat(str(start)), end=_date.fromisoformat(str(end)))
    with session_scope() as session:
        before = _snapshot(session)

    from bdp.agents.dag import get_dag

    orch = Orchestrator()
    run_id = orch.execute(get_dag("nightly"), trigger="test", params={"days": 5})

    store = StateStore()
    run = store.get_run(run_id)
    tasks = store.list_tasks(run_id)
    assert run.status == "succeeded", [(t.name, t.status, t.error) for t in tasks]

    with session_scope() as session:
        after_first = _snapshot(session)
    assert before == after_first

    # fan-out：4 个租户子任务 + fan-in 汇总 artifact
    child_names = [t.name for t in tasks if t.name.startswith("metrics#")]
    assert len(child_names) == 4
    assert all(t.status == "succeeded" for t in tasks if t.name.startswith("metrics#"))
    group = store.get_artifacts(run_id)["materialize"]
    assert sorted(group["tenants"].keys()) == ["T001", "T002", "T003", "T004"]
    assert group["failed_tenants"] == []

    # 幂等：第二次 nightly 结果一致
    run_id2 = Orchestrator().execute(get_dag("nightly"), trigger="test", params={"days": 5})
    assert StateStore().get_run(run_id2).status == "succeeded"
    with session_scope() as session:
        after_second = _snapshot(session)
    assert after_first == after_second

    # ---- 恢复共享夹具状态，保证后续测试的 DQ 样本可用 ----
    _restore_fixture_state()


# ---------------------------------------------------------------------------
# Agent API 与 InsightAgent
# ---------------------------------------------------------------------------


def test_agent_ask_degraded_without_llm(client, tokens):
    res = client.post("/v1/agent/ask", headers=auth(tokens["nova"]),
                      json={"question": "退款率最近怎么样"})
    assert res.status_code == 200
    body = res.json()
    assert body["tenant_id"] == "T001"
    assert body["degraded"] is True  # 未配置 LLM → 确定性降级
    assert "GMV" in body["answer"] or "指标" in body["answer"]
    assert any(c["type"] == "metric" for c in body["citations"])
    assert any(c["type"] == "kb" for c in body["citations"])

    # 平台级身份不带租户 → 400（与 /v1/kb/search 同一规则）
    res2 = client.post("/v1/agent/ask", headers=auth(tokens["admin"]), json={"question": "退款率"})
    assert res2.status_code == 400


def test_insight_llm_loop_with_mock(seeded, monkeypatch):
    """用假 LLM 驱动完整工具循环：数字必须来自工具、引用自动登记。"""
    import json as _json

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages, tools=None, temperature=0.2):
            self.calls += 1
            if self.calls == 1:
                return {"role": "assistant", "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "query_metric", "arguments": _json.dumps({
                        "metric_code": "GMV_PAID", "start": "2026-09-01", "end": "2026-09-25",
                    })},
                }]}
            return {"role": "assistant", "content": "支付 GMV 数值来自工具返回，口径已注明。"}

    monkeypatch.setattr("bdp.agents.llm.LLMClient", FakeLLM)
    result = run_agent_inline(
        "insight", {"question": "GMV 多少"}, scope=TenantScope.of("T001"),
    )
    assert result.status == "succeeded"
    assert result.stats["mode"] == "llm"
    insight = result.artifacts["insight"]
    assert insight["citations"] == [{
        "type": "metric", "code": "GMV_PAID", "caliber_version": "v1.1",
    }]
    assert insight["tool_trace"][0]["tool"] == "query_metric"


def test_agent_api_run_flow(client, tokens):
    # 品牌账号无权触发
    res_forbidden = client.post("/v1/agent/runs", headers=auth(tokens["nova"]),
                                json={"dag": "nightly"})
    assert res_forbidden.status_code == 403

    # 非法 DAG → 400
    res_bad = client.post("/v1/agent/runs", headers=auth(tokens["admin"]),
                          json={"dag": "nope"})
    assert res_bad.status_code == 400

    # 触发 + 轮询至终态
    res = client.post("/v1/agent/runs", headers=auth(tokens["admin"]),
                      json={"dag": "nightly", "params": {"days": 5}})
    assert res.status_code == 200
    run_id = res.json()["run_id"]

    deadline = time.time() + 90
    detail = {}
    while time.time() < deadline:
        detail = client.get(f"/v1/agent/runs/{run_id}", headers=auth(tokens["admin"])).json()
        if detail["status"] != "running":
            break
        time.sleep(0.3)
    assert detail["status"] == "succeeded", detail.get("tasks")
    assert len(detail["tasks"]) == 12  # 8 个 DAG 任务 + 4 个租户子任务
    assert "materialize" in detail["artifacts"]

    ev = client.get(f"/v1/agent/runs/{run_id}/events", headers=auth(tokens["admin"])).json()
    assert ev["events"]

    capa = client.get("/v1/agent/capabilities", headers=auth(tokens["nova"])).json()
    assert capa["workers"] >= 1
    assert any(a["name"] == "insight" for a in capa["agents"])

    # API 触发的运行同样改变了共享数据 → 恢复夹具现场
    _restore_fixture_state()
