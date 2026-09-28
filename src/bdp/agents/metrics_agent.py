"""指标物化 Agent：prepare（建字典 + 门禁 + 清理）→ 按租户 fan-out 子任务。

门禁逻辑在 prepare 中执行：DQ 通过率低于 BDP_AGENT_DQ_GATE 视为 block，
按 BDP_AGENT_DQ_GATE_ACTION 决定 skip（短路）或 degraded（带标记继续物化）。
降级不静默：结论写入 artifact / 事件 / 任务结果，API 与看板可见。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from bdp.agents import registry
from bdp.agents.errors import FatalError, GateHoldRequested
from bdp.agents.spec import AgentContext, AgentResult, AgentSpec


class MetricsInput(BaseModel):
    kind: Literal["prepare", "materialize_tenant"]


class MetricsAgent:
    spec = AgentSpec(
        name="metrics",
        write_domains=("metric",),
        timeout_sec=900,
        max_attempts=2,
        input_model=MetricsInput,
        description="指标字典维护与 metric_result 物化（prepare 门禁 + 按租户并行物化）",
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        inp = MetricsInput(**ctx.params)
        if inp.kind == "prepare":
            return self._prepare(ctx)
        return self._materialize_tenant(ctx)

    # ---- prepare -----------------------------------------------------------

    def _prepare(self, ctx: AgentContext) -> AgentResult:
        from sqlalchemy import delete, text

        from bdp.config import settings
        from bdp.models import MetricResult
        from bdp.metrics.registry import ensure_definitions

        dq = (ctx.artifacts or {}).get("dq") or {}
        verdict = dq.get("verdict", "pass")
        pass_rate = dq.get("pass_rate")
        gate = dq.get("gate", "n/a")
        blocked = verdict != "pass"

        ctx.emit(
            "info",
            f"指标门禁：pass_rate={pass_rate}，gate={gate}，{'block' if blocked else 'pass'}",
            {"dq_verdict": verdict, "gate_action": settings.agent_dq_gate_action if blocked else None},
        )

        with ctx.session() as session:
            added = ensure_definitions(session)
            row = session.execute(text("SELECT MIN(dt), MAX(dt) FROM dws_tenant_day")).one_or_none()
            if not row or row[1] is None:
                raise FatalError("没有汇总数据，请先执行 pipeline（dws 任务）")

            if blocked and settings.agent_dq_gate_action == "skip":
                return AgentResult(
                    status="skipped",
                    stats={"gate": "blocked", "action": "skip"},
                    warning=f"质量门禁阻断（pass_rate={pass_rate} < {gate}），跳过指标物化",
                )
            if blocked and settings.agent_dq_gate_action == "hold":
                # 审批闸口：物化暂停，等人工决定（approve → 重跑；reject → 短路）
                # agent 无权自己把任务置 waiting_approval——抛出专用异常，
                # 由编排器/worker 在任务层落状态（与取消信号同一协作模式）。
                raise GateHoldRequested(
                    f"质量门禁阻断（pass_rate={pass_rate} < {gate}），物化等待审批"
                )

            # 门禁通过（或 degraded 继续策略）后才清理旧物化结果
            session.execute(delete(MetricResult))
            window = (row[0], row[1])

        status = "degraded" if blocked else "succeeded"
        return AgentResult(
            status=status,
            artifacts={
                "metrics": {
                    "start": str(window[0]),
                    "end": str(window[1]),
                    "gate": "degraded" if blocked else "pass",
                    "new_definitions": added,
                }
            },
            stats={"window": f"{window[0]} ~ {window[1]}", "new_definitions": added,
                   "gate": "degraded" if blocked else "pass"},
            warning=f"质量门禁降级（pass_rate={pass_rate} < {gate}），物化结果标记为降级" if blocked else None,
        )

    # ---- 按租户物化（fan-out 子任务） ---------------------------------------

    def _materialize_tenant(self, ctx: AgentContext) -> AgentResult:
        from bdp.metrics.engine import materialize_tenant

        tenant_id = ctx.tenant_id
        if not tenant_id:
            raise FatalError("materialize_tenant 子任务必须携带 TenantScope（fan-out 产出）")

        window = (ctx.artifacts or {}).get("metrics") or {}
        if not window.get("start") or not window.get("end"):
            raise FatalError("缺少物化窗口 artifact（metrics），无法执行按租户物化")
        from datetime import date

        ctx.emit("info", f"物化租户 {tenant_id} 指标", {"window": f"{window['start']} ~ {window['end']}"})
        with ctx.session() as session:
            res = materialize_tenant(
                session, tenant_id=tenant_id,
                start=date.fromisoformat(str(window["start"])),
                end=date.fromisoformat(str(window["end"])),
            )

        errors = [d for d in res["details"] if d.get("error")]
        ctx.emit(
            "warn" if errors else "info",
            f"租户 {tenant_id} 物化完成：{res['written']} 行" + (f"，{len(errors)} 个指标失败" if errors else ""),
        )
        gate = (ctx.artifacts or {}).get("metrics", {}).get("gate")
        return AgentResult(
            status="degraded" if gate == "degraded" else "succeeded",
            artifacts={
                f"materialized:{tenant_id}": {"written": res["written"], "errors": errors}
            },
            stats={"tenant_id": tenant_id, "written": res["written"], "metric_errors": len(errors)},
            warning=f"{len(errors)} 个指标计算失败" if errors else None,
        )


registry.register(MetricsAgent())
