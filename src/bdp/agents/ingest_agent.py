"""数据接入 Agent：模拟数据生成 → raw / dim / user / kb_document。

对 nightly DAG 而言，本 agent 默认以"全量替换"模式工作：先清空自己写域内的
业务表再重新生成——这是 mock 生成器 append 语义下的幂等化处理，保证
"连续跑两次 nightly 结果一致"。接入真实平台 API 时替换 run() 内部实现即可，
输入输出契约不变。
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from bdp.agents import registry
from bdp.agents.errors import FatalError
from bdp.agents.spec import AgentContext, AgentResult, AgentSpec
from bdp.models import (
    AppUser,
    KbDocument,
    PlatformSkuMap,
    RawCsSession,
    RawOrder,
    RawRefund,
    Shop,
    Sku,
    Spu,
    Tenant,
)


class IngestInput(BaseModel):
    source: str = "mock"
    days: int | None = Field(default=None, gt=0, le=3650)
    seed: int | None = None
    replace: bool = True  # 全量替换（幂等）；False 时按生成器原始 append 语义


class IngestAgent:
    spec = AgentSpec(
        name="ingest",
        write_domains=("raw", "dim", "user", "kb_document"),
        timeout_sec=600,
        max_attempts=2,
        input_model=IngestInput,
        description="生成/接入原始数据（raw）与主数据（dim/user/kb_document）",
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        inp = IngestInput(**ctx.params)
        if inp.source != "mock":
            raise FatalError(f"暂不支持数据源 {inp.source}（预留 platform_api 扩展点）")

        from bdp.mock.generator import generate_all

        ctx.emit("info", "开始生成模拟数据", {"days": inp.days, "seed": inp.seed})
        with ctx.session() as session:
            if inp.replace:
                self._clear_domain(session)
            stats = generate_all(session, seed=inp.seed, days=inp.days)

        window_raw = stats.pop("window", "")
        window = self._parse_window(window_raw)
        ctx.emit("info", "模拟数据生成完成", {k: stats.get(k) for k in ("orders", "refunds", "cs_sessions")})

        return AgentResult(
            status="succeeded",
            artifacts={
                "ingest": {
                    "source": inp.source,
                    "seed": inp.seed,
                    "days": inp.days,
                    "window": {"start": window[0].isoformat(), "end": window[1].isoformat()}
                    if window
                    else None,
                    "counts": stats,
                }
            },
            stats=stats,
        )

    @staticmethod
    def _clear_domain(session) -> None:
        """清空接入写域内的表（models 未定义外键约束，顺序无关）。"""
        for model in (
            RawOrder, RawRefund, RawCsSession,
            PlatformSkuMap, Sku, Spu, Shop, Tenant,
            KbDocument, AppUser,
        ):
            session.query(model).delete()
        session.flush()

    @staticmethod
    def _parse_window(window_raw: str) -> tuple[date, date] | None:
        try:
            start_s, end_s = (p.strip() for p in window_raw.split("~"))
            return date.fromisoformat(start_s), date.fromisoformat(end_s)
        except (ValueError, AttributeError):
            return None


registry.register(IngestAgent())
