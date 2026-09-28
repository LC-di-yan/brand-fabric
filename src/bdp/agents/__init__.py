"""多 Agent 协作层。

设计原则（详见 docs/AGENT_REFACTOR_PLAN.md）：
1. Agent 之间零直接调用，只通过 DAG 依赖 + artifact 引用通信；
2. 消息只传引用（batch_id / 窗口 / verdict），数据本体始终在现有业务表中；
3. 租户上下文由 Orchestrator 唯一解析并以 TenantScope 强制下发，agent 内禁止二次解析；
4. 现有业务函数零改写，agent 只是"壳"：会话、守卫、重试、观测。
"""

from bdp.agents.errors import (
    FatalError,
    RetryableError,
    TaskCancelled,
    WriteDomainViolation,
)
from bdp.agents.spec import AgentContext, AgentResult, AgentSpec, TenantScope

__all__ = [
    "AgentContext",
    "AgentResult",
    "AgentSpec",
    "TenantScope",
    "FatalError",
    "RetryableError",
    "TaskCancelled",
    "WriteDomainViolation",
]
