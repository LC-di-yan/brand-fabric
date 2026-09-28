"""Agent 层异常体系：为重试与降级提供统一分类。

现有业务异常（MetricError / KbManageError）保持原继承树不动，
由 orchestrator 按类型映射为 Fatal（不可重试）；本模块只新增 agent 层自己的异常。
"""

from __future__ import annotations


class AgentError(Exception):
    """agent 层异常基类。"""

    retryable: bool = False


class RetryableError(AgentError):
    """可重试错误：外部服务（embedding / LLM / 向量库 / 网络）暂时不可用。"""

    retryable = True


class FatalError(AgentError):
    """不可重试错误：口径缺失、参数错误、数据契约违例等，重试不会好转。"""

    retryable = False


class TaskCancelled(AgentError):
    """协作式取消：任务在批次边界检测到取消/超时信号后主动退出。

    Python 线程无法被强杀，超时的实现依赖任务在阶段边界主动检查 ctx.cancel()。
    取消视为可重试（重试时从头执行，全量重建类任务天然幂等）。
    """

    retryable = True


class WriteDomainViolation(AgentError):
    """越域写：agent 尝试写声明写域之外的表，运行时强制拦截。"""

    retryable = False
