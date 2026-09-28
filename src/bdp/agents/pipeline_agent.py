"""数仓加工 Agent：dwd 三路（订单/退款/会话）与 dws 汇总。

nightly DAG 把三条明细加工拆成三个并行任务，各自有更窄的写域
（如 orders 任务只允许写 dwd_order）；build_dwd 的整体编排仍保留，
供旧 CLI 路径与单测使用。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from bdp.agents import registry
from bdp.agents.spec import AgentContext, AgentResult, AgentSpec


class PipelineInput(BaseModel):
    domain: Literal["orders", "refunds", "cs", "dws"]


class PipelineAgent:
    spec = AgentSpec(
        name="pipeline",
        write_domains=("dwd_order", "dwd_refund", "dwd_cs", "dws"),
        timeout_sec=900,
        max_attempts=2,
        input_model=PipelineInput,
        description="明细层清洗/主数据映射/质量标记与汇总层轻度聚合",
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        inp = PipelineInput(**ctx.params)
        ctx.emit("info", f"开始加工：{inp.domain}")

        if inp.domain == "orders":
            from bdp.pipeline.dwd import build_dwd_orders
            from bdp.pipeline.mdm import PlatformSkuMatcher

            with ctx.session() as session:
                matcher = PlatformSkuMatcher.build(session)
                stats = build_dwd_orders(session, matcher)
            artifacts = {"mdm": {
                "exact_index_size": matcher.exact_index_size,
                "normalized_conflicts": len(matcher.conflicts),
            }}
        elif inp.domain == "refunds":
            from bdp.pipeline.dwd import build_dwd_refunds

            with ctx.session() as session:
                stats = build_dwd_refunds(session)
            artifacts = {}
        elif inp.domain == "cs":
            from bdp.pipeline.dwd import build_dwd_cs

            with ctx.session() as session:
                stats = build_dwd_cs(session)
            artifacts = {}
        else:
            from bdp.pipeline.dws import build_dws

            with ctx.session() as session:
                stats = build_dws(session)
            artifacts = {
                "window": self._dws_window(ctx),
            }

        ctx.emit("info", f"加工完成：{inp.domain}", {"raw": stats.get("raw")})
        return AgentResult(status="succeeded", artifacts=artifacts, stats={"domain": inp.domain, **stats})

    @staticmethod
    def _dws_window(ctx: AgentContext) -> dict | None:
        """从 dws_tenant_day 取数据窗口（与旧路径 cmd_metrics 同源，保证口径一致）。"""
        from sqlalchemy import text

        with ctx.session(commit_on_exit=False) as session:
            row = session.execute(
                text("SELECT MIN(dt), MAX(dt) FROM dws_tenant_day")
            ).one_or_none()
        if not row or row[1] is None:
            return None
        return {"start": str(row[0]), "end": str(row[1])}


registry.register(PipelineAgent())
