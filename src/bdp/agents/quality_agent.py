"""数据质量门禁 Agent。

职责分离的关键：本 agent 只产出 verdict（结论 + 通过率），无权直接让运行失败；
是否阻塞下游指标物化由 orchestrator 配置（BDP_AGENT_DQ_GATE / BDP_AGENT_DQ_GATE_ACTION）
在 metrics agent 中执行。判定与动作分开，门禁策略才可调。
"""

from __future__ import annotations

from pydantic import BaseModel

from bdp.agents import registry
from bdp.agents.spec import AgentContext, AgentResult, AgentSpec


class QualityInput(BaseModel):
    pass


class QualityAgent:
    spec = AgentSpec(
        name="quality",
        write_domains=("dq",),
        timeout_sec=300,
        max_attempts=1,
        input_model=QualityInput,
        description="执行数据质量规则并给出 verdict（pass/fail），是指标物化的门禁",
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        from bdp.config import settings
        from bdp.pipeline.quality import ensure_rules, quality_summary, run_quality_checks

        ctx.emit("info", "执行数据质量规则")
        with ctx.session() as session:
            ensure_rules(session)
            results = run_quality_checks(session)
            summary = quality_summary(session)

        # 判定（policy）在本 agent：通过率低于门禁阈值即 fail；
        # 动作（skip / degraded 继续）由 metrics agent 按配置执行——判定与动作分离。
        gate = settings.agent_dq_gate
        pass_rate = summary["overall_pass_rate"]
        verdict = "pass" if pass_rate >= gate else "fail"
        error_rules = [r["rule_id"] for r in results if r["severity"] == "error" and r["failed_rows"] > 0]
        ctx.emit(
            "info",
            f"质量校验完成：verdict={verdict}（pass_rate={pass_rate}，gate={gate}）",
            {"error_rules_with_failures": error_rules, "verdict": verdict},
        )

        return AgentResult(
            status="succeeded",
            artifacts={
                "dq": {
                    "run_id": summary["run_id"],
                    "verdict": verdict,
                    "pass_rate": pass_rate,
                    "gate": gate,
                    "rules": summary["rules"],
                    "failed_rules": summary["failed_rules"],
                    "error_rules_with_failures": error_rules,
                }
            },
            stats={
                "rules_run": len(results),
                "failed_rules": summary["failed_rules"],
                "overall_pass_rate": pass_rate,
                "verdict": verdict,
            },
        )


registry.register(QualityAgent())
