"""知识库 Agent：向量库全量重建（nightly）与检索能力持有方。

rebuild 是幂等全量操作（store.reset + 切片重建）；检索（query）不作为队列任务——
它走同步 API 路径（/v1/kb/search 与 InsightAgent 的工具调用），保证交互延迟。
本 agent 的检索隔离规则沿用 retriever 的硬约束：知识检索不接受无租户调用。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from bdp.agents import registry
from bdp.agents.errors import FatalError, RetryableError
from bdp.agents.spec import AgentContext, AgentResult, AgentSpec


class KbInput(BaseModel):
    kind: Literal["rebuild"] = "rebuild"
    tenant_id: str | None = None


class KbAgent:
    spec = AgentSpec(
        name="kb",
        write_domains=("kb_chunk",),
        timeout_sec=600,
        max_attempts=3,
        input_model=KbInput,
        description="知识库切片入库与向量索引重建（向量库属本 agent 独立数据域）",
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        from bdp.kb.ingest import ingest_knowledge
        from bdp.kb.store import default_store

        inp = KbInput(**ctx.params)
        tenant_id = inp.tenant_id or ctx.tenant_id
        ctx.emit("info", "重建知识库向量索引", {"tenant_id": tenant_id or "ALL"})

        try:
            with ctx.session() as session:
                stats = ingest_knowledge(
                    session, store=default_store(), tenant_id=tenant_id, rebuild=True
                )
        except RetryableError:
            raise
        except Exception as exc:
            # embedding 后端（bge/openai）与网络相关故障按可重试处理；
            # 维度不一致等数据问题重试同样不会好转，重试耗尽后进死信。
            if "维度不一致" in str(exc):
                raise FatalError(f"知识库重建失败：{exc}") from exc
            raise RetryableError(f"知识库重建失败：{exc}") from exc

        ctx.emit(
            "info",
            f"知识库重建完成：{stats.get('chunks', 0)} 切片 / 写入 {stats.get('written_to_vector_store', 0)}",
        )
        return AgentResult(
            status="succeeded",
            artifacts={
                "kb": {
                    "documents": stats.get("documents"),
                    "chunks": stats.get("chunks"),
                    "dedup_skipped": stats.get("dedup_skipped"),
                    "embedding_backend": stats.get("embedding_backend"),
                    "vector_backend": stats.get("vector_backend"),
                }
            },
            stats={k: v for k, v in stats.items() if isinstance(v, (int, float, str))},
        )


registry.register(KbAgent())
