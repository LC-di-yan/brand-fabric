"""VerifierAgent：只读结论复核（P2 生产者-复核者协作）。

回答的问题："这次物化可信吗？"——DQ 门禁拦的是输入侧（数据脏不脏），
Verifier 拦的是输出侧（算出来的东西对不对）：

1. 行数水位：metric_result 今日行数 vs 历史 run 均值 ± 容差——骤降/暴涨都是事故信号；
2. 口径一致性：物化记录的 caliber_version 必须能在指标字典里找到（防止幽灵口径）；
3. 负值扫描：GMV/金额类指标结果不应为负（引擎兜底失效的信号）。

write_domains=()：纯只读，任何写库行为会被 GuardedSession 当场拦截（有测试）。
verdict=fail 时只产出结论，处置（标 degraded / 告警）由编排器决定——
与 quality_agent 的"只产 verdict，动作归编排"同一协作契约。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from sqlalchemy import func, select

from bdp.agents import registry
from bdp.agents.errors import FatalError, RetryableError
from bdp.agents.spec import AgentContext, AgentResult, AgentSpec
from bdp.models import MetricDef, MetricResult

# 行数水位容差：今日物化行数低于历史均值 50% 或高于 200% 视为异常
_LEVEL_LOW = 0.5
_LEVEL_HIGH = 2.0


class VerifierInput(BaseModel):
    kind: Literal["verify"] = "verify"
    # caliber_diff 模板透传：聚焦对账该指标的口径（None = 全量扫描）
    metric_code: str | None = None


class VerifierAgent:
    spec = AgentSpec(
        name="verifier",
        write_domains=(),  # 纯只读
        timeout_sec=120,
        max_attempts=2,
        input_model=VerifierInput,
        description="只读结论复核：行数水位 / 口径一致性 / 负值扫描，fail 阻断下游",
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        inp = VerifierInput(**ctx.params)
        findings: list[dict] = []
        try:
            with ctx.session(commit_on_exit=False) as session:
                findings.extend(self._check_volume(session))
                findings.extend(self._check_caliber(session, inp.metric_code))
                findings.extend(self._check_negative(session))
        except FatalError:
            raise
        except Exception as exc:
            raise RetryableError(f"复核查询失败：{exc}") from exc

        failed = [f for f in findings if f["severity"] == "error"]
        warns = [f for f in findings if f["severity"] == "warn"]
        verdict = "fail" if failed else ("warn" if warns else "pass")

        ctx.emit("info" if verdict == "pass" else "warn",
                 f"复核结论 {verdict}：{len(failed)} error / {len(warns)} warn")

        return AgentResult(
            status="succeeded" if verdict == "pass" else "degraded",
            artifacts={"verify": {
                "verdict": verdict,
                "findings": findings,
                "failed": len(failed),
                "warned": len(warns),
            }},
            stats={"verdict": verdict, "findings": len(findings)},
            warning=None if verdict == "pass" else f"复核 {verdict}：{failed[0]['check'] if failed else warns[0]['check']}",
        )

    def _check_volume(self, session) -> list[dict]:
        """行数水位：最近物化日的行数 vs 前几日均值（metric_result 按 dt 归集）。"""
        rows = session.execute(
            select(MetricResult.dt, func.count())
            .group_by(MetricResult.dt)
            .order_by(MetricResult.dt.desc())
            .limit(7)
        ).all()
        if len(rows) < 3:
            return []
        latest_dt, latest_cnt = rows[0]
        hist = [c for _, c in rows[1:]]
        avg = sum(hist) / len(hist)
        if avg <= 0:
            return []
        findings = []
        if latest_cnt < avg * _LEVEL_LOW:
            findings.append({
                "check": "volume", "severity": "error",
                "detail": f"物化行数骤降：{latest_dt} 共 {latest_cnt} 行，"
                          f"前 {len(hist)} 日均值 {avg:.0f} 行（低于 {int(_LEVEL_LOW * 100)}% 水位）",
            })
        elif latest_cnt > avg * _LEVEL_HIGH:
            findings.append({
                "check": "volume", "severity": "warn",
                "detail": f"物化行数异常放大：{latest_dt} 共 {latest_cnt} 行，"
                          f"前 {len(hist)} 日均值 {avg:.0f} 行",
            })
        return findings

    def _check_caliber(self, session, metric_code: str | None = None) -> list[dict]:
        """口径一致性：物化里的 (code, version) 必须存在于字典。

        metric_code 传入时聚焦对账该指标（caliber_diff 模板用法）。
        """
        stmt = select(MetricResult.metric_code, MetricResult.caliber_version).distinct()
        if metric_code:
            stmt = stmt.where(MetricResult.metric_code == metric_code)
        used = session.execute(stmt).all()
        known = set(session.execute(
            select(MetricDef.metric_code, MetricDef.caliber_version)
        ).all())
        ghosts = [u for u in used if u not in known]
        if not ghosts:
            return []
        return [{
            "check": "caliber", "severity": "error",
            "detail": f"物化结果使用了字典中不存在的口径：{ghosts[:5]}",
        }]

    def _check_negative(self, session) -> list[dict]:
        """负值扫描：金额类指标存在任何负值点位即告警。

        不用 SUM——总量会被正常正数稀释，单个负值点位根本看不出来。
        """

        amount_codes = ("GMV_ORDER", "GMV_PAID", "GMV_SETTLE", "REFUND_AMOUNT")
        findings = []
        for code in amount_codes:
            min_val = session.execute(
                select(func.min(MetricResult.value)).where(MetricResult.metric_code == code)
            ).scalar()
            if min_val is not None and min_val < 0:
                findings.append({
                    "check": "negative", "severity": "error",
                    "detail": f"指标 {code} 存在负值点位（min={min_val}），引擎兜底可能失效",
                })
        return findings


registry.register(VerifierAgent())
