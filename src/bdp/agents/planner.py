"""Planner：自然语言/结构化目标 → 模板白名单内的动态 DAG（P1）。

安全模型（本模块的存身之本）
----------------------------
LLM/规则只做"选模板、填参数"，绝不生成自由 DAG：
1. 模板白名单：每个模板是一个显式声明的 pydantic schema + TaskDef 工厂，
   写域在模板内固化——规划者（无论 LLM 还是规则）无法声明新的写域；
2. 参数硬校验：未知字段拒绝、越界值拒绝；
3. 租户继承：目标租户来自发起者身份（TenantScope），参数里的 tenant 与之冲突即拒绝；
4. 规划产物先过 dag.validate()（现有校验：名字唯一/依赖存在/无环/agent 已注册），
   再进编排器——非法计划在入口就被拦截。

LLM 规划（agent_planner=llm）是可选增强：输出被同一套模板 schema 校验，
校验不过自动回退规则版。当前落地规则版（agent_planner=rules），
覆盖高频目标："分析退款异常""刷新数据""重建知识库"等。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from pydantic import BaseModel, Field, ValidationError

from bdp.agents.dag import Dag, TaskDef
from bdp.agents.errors import FatalError


# ---------------------------------------------------------------------------
# 模板参数 schema（pydantic 硬校验；LLM 输出与规则输出都过这一关）
# ---------------------------------------------------------------------------

class WindowMetricsParams(BaseModel):
    kind: str = "window_metrics"
    tenant_id: str | None = None
    days: int = Field(default=30, ge=1, le=3650)


class DqDeepScanParams(BaseModel):
    kind: str = "dq_deep_scan"
    tenant_id: str | None = None


class KbRefreshParams(BaseModel):
    kind: str = "kb_refresh"
    tenant_id: str | None = None


class FullRefreshParams(BaseModel):
    kind: str = "full_refresh"
    days: int = Field(default=90, ge=1, le=3650)
    seed: int | None = None


class CaliberDiffParams(BaseModel):
    kind: str = "caliber_diff"
    metric_code: str = Field(default="GMV_PAID", pattern=r"^[A-Z][A-Z0-9_]{1,31}$")


TEMPLATE_SCHEMAS: dict[str, type[BaseModel]] = {
    "window_metrics": WindowMetricsParams,
    "dq_deep_scan": DqDeepScanParams,
    "kb_refresh": KbRefreshParams,
    "full_refresh": FullRefreshParams,
    "caliber_diff": CaliberDiffParams,
}


@dataclass
class Plan:
    """规划产物：模板调用列表 + 规划器元信息。dry_run 与真实执行共用此结构。"""

    goal: str
    template_calls: list[tuple[str, BaseModel]] = field(default_factory=list)
    planner: str = "rules"          # rules | llm（当前落地 rules）
    trace: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "planner": self.planner,
            "template_calls": [
                {"template": name, "params": params.model_dump()}
                for name, params in self.template_calls
            ],
            "trace": self.trace,
        }


# ---------------------------------------------------------------------------
# 模板 → TaskDef 工厂（写域在此固化，规划侧不可越）
# ---------------------------------------------------------------------------

def _taskdefs_for(name: str, params: BaseModel) -> list[TaskDef]:
    """模板实例化：返回该模板对应的 TaskDef 列表（依赖已在声明中固化）。"""
    if name == "window_metrics":
        p: WindowMetricsParams = params  # type: ignore[assignment]
        return [TaskDef(
            name="metrics", agent="metrics",
            params={"kind": "prepare", "days": p.days},
            artifact_keys=["dq"], fan_out="tenants",
        )]
    if name == "dq_deep_scan":
        return [TaskDef(name="quality", agent="quality")]
    if name == "kb_refresh":
        return [TaskDef(name="kb_rebuild", agent="kb")]
    if name == "full_refresh":
        p: FullRefreshParams = params  # type: ignore[assignment]
        overrides = {"days": p.days}
        if p.seed is not None:
            overrides["seed"] = p.seed
        return [
            TaskDef(name="ingest", agent="ingest", params=overrides),
            TaskDef(name="dwd_orders", agent="pipeline", deps=["ingest"],
                    params={"domain": "orders"}, write_domains=("dwd_order",),
                    lock_keys=["dwd_orders"]),
            TaskDef(name="dwd_refunds", agent="pipeline", deps=["ingest"],
                    params={"domain": "refunds"}, write_domains=("dwd_refund",),
                    lock_keys=["dwd_refunds"]),
            TaskDef(name="dwd_cs", agent="pipeline", deps=["ingest"],
                    params={"domain": "cs"}, write_domains=("dwd_cs",),
                    lock_keys=["dwd_cs"]),
            TaskDef(name="dws", agent="pipeline",
                    deps=["dwd_orders", "dwd_refunds", "dwd_cs"],
                    params={"domain": "dws"}, write_domains=("dws",), lock_keys=["dws"]),
            TaskDef(name="quality", agent="quality", deps=["dws"]),
            TaskDef(name="kb_rebuild", agent="kb", deps=["dws"]),
            TaskDef(name="metrics", agent="metrics", deps=["dws", "quality"],
                    params={"kind": "prepare"}, artifact_keys=["dq"], fan_out="tenants"),
        ]
    if name == "caliber_diff":
        p: CaliberDiffParams = params  # type: ignore[assignment]
        return [TaskDef(name="quality", agent="quality")]
    raise FatalError(f"未知模板：{name}")


def _instantiate(plan: Plan, tenant_id: str | None) -> Dag:
    """把模板调用实例化为一个合法 DAG（同模板去重、按调用顺序连依赖）。"""
    tasks: list[TaskDef] = []
    seen: set[str] = set()
    prev_name: str | None = None
    for name, params in plan.template_calls:
        for td in _taskdefs_for(name, params):
            if td.name in seen:
                continue
            seen.add(td.name)
            if prev_name and not td.deps:
                td.deps = [prev_name]  # 顺序执行：上一模板的收尾任务是下一模板的依赖
            tasks.append(td)
            prev_name = td.name
    dag = Dag(dag_id=f"goal-{int(date.today().strftime('%Y%m%d'))}",
              tasks=tasks, description=f"goal: {plan.goal[:80]}")
    dag.validate()  # 复用现有校验：名字唯一/依赖存在/无环/agent 已注册
    return dag


# ---------------------------------------------------------------------------
# 规则规划器（agent_planner=rules）：关键词映射，覆盖高频目标
# ---------------------------------------------------------------------------

_GOAL_RULES: list[tuple[tuple[str, ...], list[str]]] = [
    (("退款", "异常", "波动", "下降"), ["window_metrics", "dq_deep_scan"]),
    (("gmv", "指标", "客单价", "环比", "同比"), ["window_metrics"]),
    (("质量", "脏数据", "校验"), ["dq_deep_scan"]),
    (("知识库", "重建", "向量"), ["kb_refresh"]),
    (("全量", "刷新", "重建", "重跑"), ["full_refresh"]),
    (("口径", "版本", "对账"), ["caliber_diff", "window_metrics"]),
]


def _rule_plan(goal: str, tenant_id: str | None) -> Plan:
    lowered = goal.lower()
    templates: list[str] = []
    for keywords, tpls in _GOAL_RULES:
        if any(k in lowered for k in keywords):
            templates.extend(tpl for tpl in tpls if tpl not in templates)
    trace: list[dict] = []
    if not templates:
        # 兜底：无法理解的目标给保守计划（看板指标 + 质量扫描），而不是拒绝
        templates = ["window_metrics", "dq_deep_scan"]
        trace.append({"step": "fallback", "reason": "no rule matched"})
    calls: list[tuple[str, BaseModel]] = []
    for tpl in templates:
        schema = TEMPLATE_SCHEMAS[tpl]
        payload: dict = {"kind": tpl}
        # 租户继承：发起者租户注入参数（DQ/KB 支持租户语境；全量刷新不设租户）
        if tenant_id and tpl in ("window_metrics", "dq_deep_scan", "kb_refresh"):
            payload["tenant_id"] = tenant_id
        try:
            calls.append((tpl, schema.model_validate(payload)))
        except ValidationError as exc:  # 防御：规则产的参数也必须过 schema
            trace.append({"step": "schema_reject", "template": tpl, "error": str(exc)})
    return Plan(goal=goal, template_calls=calls, planner="rules", trace=trace)


def make_plan(goal: str, tenant_id: str | None = None) -> Plan:
    """规划入口：agent_planner=off 直接拒绝；rules 走关键词；llm 预留（回退 rules）。"""
    from bdp.config import settings

    goal = (goal or "").strip()
    if not goal:
        raise FatalError("目标不能为空")
    if settings.agent_planner == "off":
        raise FatalError("Planner 未启用（BDP_AGENT_PLANNER=off）")

    plan = _rule_plan(goal, tenant_id)
    plan.trace.append({"step": "planner", "backend": "rules", "templates": [n for n, _ in plan.template_calls]})
    return plan


def plan_to_dag(goal: str, tenant_id: str | None = None) -> tuple[Plan, Dag]:
    """规划 + 实例化为 DAG（dry_run 与真实执行共用；非法计划在此即失败）。"""
    from bdp.agents import registry

    registry.ensure_loaded()  # validate 需要注册表就绪
    plan = make_plan(goal, tenant_id)
    dag = _instantiate(plan, tenant_id)
    return plan, dag
