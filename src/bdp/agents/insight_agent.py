"""InsightAgent：业务问答（多 agent 系统中唯一需要 LLM 的角色）。

三条硬约束（写在系统提示与实现两处）：
1. 答案中的数字一律来自工具返回，LLM 不生成任何数值；
2. 租户由 ctx 注入工具层，LLM 的参数里没有 tenant 维度，无法发起跨租户查询；
3. 引用随工具调用同步登记（指标带口径版本，知识带文档切片），可溯源。

降级路径（LLM 未配置 / 超时 / 限流）：返回"指标卡片 + 检索片段"的确定性结果，
degraded=true 显式标记——这是本项目"静默降级必须消灭"原则的落地。
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta

from pydantic import BaseModel

from bdp.agents import registry
from bdp.agents.errors import FatalError
from bdp.agents.spec import AgentContext, AgentResult, AgentSpec

MAX_TOOL_ROUNDS = 8
DEFAULT_METRICS = ("GMV_PAID", "REFUND_RATE", "AOV_PAID", "CS_SATISFACTION_AVG")

SYSTEM_PROMPT = """你是多品牌电商数据中台的业务分析助手。规则：
1. 所有数字必须来自工具返回结果，禁止自己计算或编造数值；
2. 引用指标时必须带上口径版本号（caliber_version）与指标定义；
3. 引用知识时必须注明来源文档（doc_id）；
4. 只分析当前租户的数据，不做跨品牌对比；
5. 用简洁中文回答，先给结论再给依据。"""

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "query_metric",
            "description": "查询指标序列与同比/环比。返回 points、total 与口径版本。",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric_code": {"type": "string",
                                    "description": "指标代码，如 GMV_PAID / REFUND_RATE / CS_SATISFACTION_AVG"},
                    "start": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
                    "end": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
                    "compare": {"type": "string", "enum": ["none", "mom", "yoy"],
                                "description": "环比/同比，默认 none"},
                    "dim_type": {"type": "string", "enum": ["tenant", "shop", "platform"],
                                 "description": "维度类型，默认 tenant"},
                },
                "required": ["metric_code", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_kb",
            "description": "检索当前品牌的客服/商品/政策/SOP 知识库。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "kb_type": {"type": "string", "enum": ["cs_faq", "product", "policy", "sop"]},
                    "top_k": {"type": "integer", "description": "返回条数，默认 3"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dq_summary",
            "description": "查询最近一次数据质量校验汇总（回答数据可信度问题时使用）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class InsightInput(BaseModel):
    question: str


def _default_window(session_factory, days: int = 30) -> tuple[date, date]:
    """以数据窗口为基准（而非今天），保证离线环境也能回答。"""
    from sqlalchemy import text

    try:
        with session_factory(commit_on_exit=False) as session:
            row = session.execute(text("SELECT MAX(dt) FROM dws_tenant_day")).scalar()
        if row:
            end = date.fromisoformat(str(row))
            return end - timedelta(days=days - 1), end
    except Exception:
        pass
    today = date.today()
    return today - timedelta(days=days - 1), today


def _tool_executor(ctx: AgentContext, citations: list[dict], tool_trace: list[dict]) -> dict:
    """构造绑定租户上下文的工具实现。

    租户不出现在 LLM 可见的参数中——由 ctx 注入，跨租户查询在签名层面不可达。
    每次调用同时登记引用与轨迹。
    """

    def query_metric(args: dict) -> dict:
        from bdp.metrics.engine import MetricError, compute_with_compare
        from bdp.metrics.registry import resolve_version

        code = str(args.get("metric_code") or "")
        start = date.fromisoformat(str(args.get("start")))
        end = date.fromisoformat(str(args.get("end")))
        with ctx.session(commit_on_exit=False) as session:
            definition = resolve_version(session, code, end)
            if definition is None:
                result = {"error": f"指标 {code} 在 {end} 无生效口径"}
            else:
                try:
                    res = compute_with_compare(
                        session, code,
                        tenant_id=ctx.tenant_id,
                        dim_type=str(args.get("dim_type") or "tenant"),
                        start=start, end=end,
                        compare=str(args.get("compare") or "none"),
                    )
                except MetricError as exc:
                    result = {"error": str(exc)}
                else:
                    result = {
                        "metric_code": res["metric_code"], "metric_name": res["metric_name"],
                        "caliber_version": res["caliber_version"], "definition": res["definition"],
                        "unit": res["unit"], "total": res["total"],
                        "compare": res.get("compare"), "points": res["points"],
                    }
                    citations.append({
                        "type": "metric", "code": res["metric_code"],
                        "caliber_version": res["caliber_version"],
                    })
        tool_trace.append({"tool": "query_metric", "args": args,
                           "ok": "error" not in result})
        return result

    def search_kb(args: dict) -> dict:
        from bdp.kb.retriever import search

        with ctx.session(commit_on_exit=False) as session:
            result = search(
                session,
                query=str(args.get("query") or ""),
                tenant_id=ctx.tenant_id,  # 无租户会被 retriever 硬拒绝
                kb_type=args.get("kb_type"),
                top_k=int(args.get("top_k") or 3),
            )
        hits = result.get("results", [])
        for r in hits:
            citations.append({
                "type": "kb", "doc_id": r["doc_id"], "kb_type": r["kb_type"],
                "chunk_ix": r["chunk_ix"],
            })
        tool_trace.append({"tool": "search_kb", "args": args, "ok": True, "hits": len(hits)})
        return {
            "results": [
                {"doc_id": r["doc_id"], "kb_type": r["kb_type"], "chunk_ix": r["chunk_ix"],
                 "text": r["text"][:400], "score": r["score"]}
                for r in hits
            ],
            "warning": result.get("warning"),
        }

    def dq_summary(args: dict) -> dict:
        from bdp.pipeline.quality import quality_summary

        with ctx.session(commit_on_exit=False) as session:
            summary = quality_summary(session)
        out = {k: summary[k] for k in ("run_id", "rules", "failed_rules", "overall_pass_rate")}
        tool_trace.append({"tool": "dq_summary", "args": args, "ok": True})
        return out

    return {"query_metric": query_metric, "search_kb": search_kb, "dq_summary": dq_summary}


def _degraded_answer(ctx: AgentContext, question: str, start: date, end: date,
                     reason: str) -> AgentResult:
    """LLM 不可用时的确定性回答：默认指标卡片 + 检索片段，degraded 显式标记。"""
    citations: list[dict] = []
    tool_trace: list[dict] = []
    tools = _tool_executor(ctx, citations, tool_trace)

    cards = []
    for code in DEFAULT_METRICS:
        res = tools["query_metric"]({
            "metric_code": code, "start": start.isoformat(),
            "end": end.isoformat(), "compare": "mom",
        })
        if "error" not in res:
            cards.append({
                "metric_code": res["metric_code"], "metric_name": res["metric_name"],
                "caliber_version": res["caliber_version"], "unit": res["unit"],
                "total": res["total"],
                "mom_delta_pct": (res.get("compare") or {}).get("delta_pct"),
            })
    kb_hits = tools["search_kb"]({"query": question, "top_k": 3}).get("results", [])

    lines = [f"（智能问答降级模式：{reason}）", "", "近 30 天关键指标："]
    for c in cards:
        mom = c["mom_delta_pct"]
        mom_text = f"，环比 {mom:+.1%}" if mom is not None else ""
        lines.append(f"- {c['metric_name']}：{c['total']} {c['unit']}"
                     f"（口径 {c['caliber_version']}{mom_text}）")
    if kb_hits:
        lines.append("")
        lines.append("相关知识：")
        for r in kb_hits[:3]:
            lines.append(f"- [{r['kb_type']}/{r['doc_id']}#{r['chunk_ix']}] {r['text'][:80]}")

    return AgentResult(
        status="degraded",
        artifacts={"insight": {
            "degraded": True, "reason": reason, "question": question,
            "answer": "\n".join(lines), "citations": citations,
            "cards": cards, "kb_refs": kb_hits, "tool_trace": tool_trace,
        }},
        stats={"mode": "degraded", "cards": len(cards), "kb_hits": len(kb_hits)},
        warning=reason,
    )


class InsightAgent:
    spec = AgentSpec(
        name="insight",
        write_domains=("insight",),
        timeout_sec=120,
        max_attempts=1,
        input_model=InsightInput,
        description="业务问答：组合指标查询/知识检索/质量汇总，答案强制引用口径与来源",
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        from bdp.agents.llm import LLMUnavailable, LLMClient, tool_call_arguments

        inp = InsightInput(**ctx.params)
        question = inp.question.strip()
        if not question:
            raise FatalError("问题不能为空")
        if not ctx.tenant_id:
            # 与 /v1/kb/search 同一规则：知识与分析与品牌强绑定，平台级聚合问答不开放
            raise FatalError("业务问答必须指定租户（品牌语境）")

        start, end = _default_window(ctx.session_factory)
        citations: list[dict] = []
        tool_trace: list[dict] = []
        tools = _tool_executor(ctx, citations, tool_trace)

        try:
            llm = LLMClient()
        except LLMUnavailable as exc:
            return _degraded_answer(ctx, question, start, end, str(exc))

        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": f"当前租户：{ctx.tenant_id}。数据窗口：{start} ~ {end}。\n问题：{question}"},
        ]

        try:
            for _ in range(MAX_TOOL_ROUNDS):
                message = llm.chat(messages, tools=TOOLS_SCHEMA)
                calls = tool_call_arguments(message)
                if not calls:
                    return self._finish(ctx, question, str(message.get("content") or ""),
                                        citations, tool_trace, started=time.time(),
                                        degraded=False)
                messages.append(message)
                for call_id, name, args in calls:
                    fn = tools.get(name)
                    if fn is None:
                        result: dict = {"error": f"未知工具 {name}"}
                    else:
                        try:
                            result = fn(args)
                        except Exception as exc:
                            result = {"error": f"工具执行失败：{exc}"}
                    messages.append({
                        "role": "tool", "tool_call_id": call_id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    })
            return self._finish(ctx, question, "（已达工具调用轮次上限，请缩小问题范围）",
                                citations, tool_trace, started=time.time(), degraded=True)
        except LLMUnavailable as exc:
            return _degraded_answer(ctx, question, start, end, str(exc))

    def _finish(self, ctx: AgentContext, question: str, answer: str, citations: list[dict],
                tool_trace: list[dict], *, started: float, degraded: bool) -> AgentResult:
        with ctx.session() as session:
            from bdp.models import InsightLog

            session.add(InsightLog(
                tenant_id=ctx.tenant_id or "",
                question=question, answer=answer,
                citations=citations, tool_trace=tool_trace,
                degraded=degraded,
                elapsed_ms=int((time.time() - started) * 1000),
            ))

        return AgentResult(
            status="degraded" if degraded else "succeeded",
            artifacts={"insight": {
                "degraded": degraded, "question": question, "answer": answer,
                "citations": citations, "tool_trace": tool_trace,
            }},
            stats={"mode": "llm_round_limit" if degraded else "llm",
                   "tool_calls": len(tool_trace)},
            warning="达到工具轮次上限" if degraded else None,
        )


registry.register(InsightAgent())
