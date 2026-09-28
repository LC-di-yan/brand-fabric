"""Agent 的静态规格与运行时上下文：拆分边界的代码化表达。

AgentSpec 是"职责边界"的声明：
- write_domains  由 GuardedSession 在运行时强制（越域写即抛异常）；
- input_model    用 pydantic 校验任务输入，坏输入在执行前失败；
- timeout / max_attempts 由 orchestrator 执行。

TenantScope 是租户上下文的唯一合法载体：只能由 orchestrator 产出，
agent 拿到的是"已解析、已放行"的租户，无法接触原始请求头。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Callable, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class TenantScope:
    """已解析的租户上下文。tenant_id=None 仅限显式声明的平台级任务。"""

    tenant_id: str | None

    @classmethod
    def platform(cls) -> "TenantScope":
        return cls(tenant_id=None)

    @classmethod
    def of(cls, tenant_id: str) -> "TenantScope":
        return cls(tenant_id=tenant_id)


@dataclass(frozen=True)
class AgentSpec:
    name: str
    write_domains: tuple[str, ...]
    timeout_sec: int = 900
    max_attempts: int = 1
    input_model: type[BaseModel] | None = None
    description: str = ""


@dataclass
class AgentContext:
    """一次任务执行的运行时上下文。

    session_factory() 产出带写域守卫的会话上下文管理器（with 语义，退出即提交/回滚）；
    cancel() 是协作式取消探针，任务必须在批次边界轮询；
    emit() 同时承担事件上报与心跳更新；
    artifacts 是上游任务写入黑板的引用（由 orchestrator 按 TaskDef.artifact_keys 装配）。
    """

    run_id: str
    task_id: str
    task_name: str
    scope: TenantScope
    params: dict[str, Any]
    artifacts: dict[str, Any]
    window: tuple[date, date] | None
    session_factory: Callable[..., Any]
    cancel: Callable[[], bool]
    emit: Callable[..., None]

    def session(self, *, commit_on_exit: bool = True):
        """打开一个带写域守卫的会话上下文。用法：with ctx.session() as s: ..."""
        return self.session_factory(commit_on_exit=commit_on_exit)

    def check_cancel(self) -> None:
        """在批次/租户/文档等阶段边界调用：收到取消信号即抛 TaskCancelled。"""
        if self.cancel():
            from bdp.agents.errors import TaskCancelled

            raise TaskCancelled(f"任务 {self.task_name} 在阶段边界被取消（超时或人工停止）")

    @property
    def tenant_id(self) -> str | None:
        return self.scope.tenant_id


@dataclass
class AgentResult:
    """任务的执行结果。

    artifacts 写入运行级黑板供下游引用；stats 供看板与 CLI 展示；
    status=degraded 表示"带标记地降级完成"（沿用本项目"不丢行只打标"的哲学）。
    """

    status: Literal["succeeded", "degraded", "skipped", "failed"] = "succeeded"
    artifacts: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    warning: str | None = None
    retryable: bool = False


def table_names_for_domains(domains: tuple[str, ...] | list[str]) -> set[str]:
    """把声明式写域展开成表名集合（写域注册表，与 GuardedSession 配合）。"""
    tables: set[str] = set()
    for d in domains:
        tables |= DOMAIN_TABLES.get(d, set())
    unknown = [d for d in domains if d not in DOMAIN_TABLES]
    if unknown:
        from bdp.agents.errors import FatalError

        raise FatalError(f"未知写域：{unknown}（请在 DOMAIN_TABLES 注册）")
    return tables


# 写域注册表：域 → 允许写的表。新增 agent 先在这里登记边界。
DOMAIN_TABLES: dict[str, set[str]] = {
    "raw": {"raw_order", "raw_refund", "raw_cs_session"},
    "dim": {"dim_tenant", "dim_shop", "dim_spu", "dim_sku", "map_platform_sku"},
    "user": {"app_user"},
    "dwd_order": {"dwd_order"},
    "dwd_refund": {"dwd_refund"},
    "dwd_cs": {"dwd_cs_session"},
    "dws": {"dws_shop_day", "dws_tenant_day"},
    "dq": {"dq_rule", "dq_result"},
    "metric": {"metric_def", "metric_result"},
    "metric_result": {"metric_result"},
    "kb_chunk": {"kb_chunk"},
    "kb_document": {"kb_document", "kb_chunk"},
    "insight": {"insight_log"},
}
