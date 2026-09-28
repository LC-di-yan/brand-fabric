"""Agent 注册表：名字 → agent 实例。orchestrator 与 CLI 都从这里发现能力。"""

from __future__ import annotations

from typing import Protocol

from bdp.agents.errors import FatalError
from bdp.agents.spec import AgentContext, AgentResult, AgentSpec


class Agent(Protocol):
    spec: AgentSpec

    def run(self, ctx: AgentContext) -> AgentResult: ...


_REGISTRY: dict[str, Agent] = {}


def register(agent: Agent) -> Agent:
    _REGISTRY[agent.spec.name] = agent
    return agent


def get(name: str) -> Agent:
    agent = _REGISTRY.get(name)
    if agent is None:
        raise FatalError(f"未注册的 agent：{name}（已注册：{sorted(_REGISTRY)}）")
    return agent


def all_specs() -> list[AgentSpec]:
    return [a.spec for a in _REGISTRY.values()]


def ensure_loaded() -> None:
    """触发全部内置 agent 的注册（惰性导入避免循环依赖）。"""
    from bdp.agents import (  # noqa: F401
        ingest_agent,
        insight_agent,
        kb_agent,
        metrics_agent,
        pipeline_agent,
        quality_agent,
        verifier_agent,
    )
