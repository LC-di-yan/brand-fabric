"""命令行入口：python -m bdp.cli <command>

所有命令都同时支持 lite 模式（SQLite + 本地向量索引）与 full 模式
（PostgreSQL + Milvus），通过 .env 中的 BDP_MODE / BDP_VECTOR_BACKEND 切换。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date

from sqlalchemy import text

from bdp.config import settings
from bdp.db import init_db, session_scope


def _print(title: str, obj) -> None:
    print(f"\n=== {title} ===")
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    else:
        print(obj)


# ---------------------------------------------------------------------------


def cmd_init(args) -> int:
    if args.drop:
        print("⚠️  即将删除全部本地数据表并重建（仅用于本地环境）")
    init_db(drop=args.drop)
    _print("初始化完成", {
        "mode": settings.mode,
        "database": settings.database_url,
        "vector_backend": settings.vector_backend,
        "recreated": args.drop,
    })
    return 0


def cmd_mock(args) -> int:
    from bdp.mock.generator import generate_all

    started = time.time()
    with session_scope() as session:
        stats = generate_all(session, seed=args.seed, days=args.days)
    stats["elapsed_sec"] = round(time.time() - started, 2)
    _print("模拟数据生成完成", stats)
    return 0


def cmd_pipeline(args) -> int:
    from bdp.pipeline.dwd import build_dwd
    from bdp.pipeline.dws import build_dws
    from bdp.pipeline.quality import ensure_rules, quality_summary, run_quality_checks

    started = time.time()
    with session_scope() as session:
        dwd_stats = build_dwd(session)
    with session_scope() as session:
        dws_stats = build_dws(session)
    with session_scope() as session:
        added = ensure_rules(session)
        dq = run_quality_checks(session)
        summary = quality_summary(session)

    _print("DWD 明细层", dwd_stats)
    _print("DWS 汇总层", dws_stats)
    _print("数据质量校验", {
        "new_rules": added,
        "rules_run": len(dq),
        "failed_rules": [r for r in dq if r["failed_rows"] > 0],
        "overall_pass_rate": summary["overall_pass_rate"],
    })
    _print("全链路耗时", f"{round(time.time() - started, 2)} 秒")
    return 0


def cmd_metrics(args) -> int:
    from bdp.metrics.engine import materialize
    from bdp.metrics.registry import ensure_definitions, list_metrics

    with session_scope() as session:
        added = ensure_definitions(session)
        defs = list_metrics(session)
        end = session.execute(text("SELECT MAX(dt) FROM dws_tenant_day")).scalar()
        start = session.execute(text("SELECT MIN(dt) FROM dws_tenant_day")).scalar()
        if not end:
            print("没有汇总数据，请先执行 pipeline")
            return 1
        start_d = date.fromisoformat(str(start))
        end_d = date.fromisoformat(str(end))
        result = materialize(session, start=start_d, end=end_d)

    _print("指标字典", {"new_definitions": added, "total": len(defs)})
    _print("指标物化", {k: v for k, v in result.items() if k != "details"})
    return 0


def cmd_search(args) -> int:
    from bdp.kb.retriever import search
    from bdp.kb.store import build_store

    store = build_store(args.backend) if args.backend else None
    with session_scope() as session:
        result = search(
            session, query=args.query, tenant_id=args.tenant_id,
            kb_type=args.kb_type, top_k=args.top_k, store=store, mode=args.mode,
        )
    if getattr(args, "json", False):
        _print(f"检索：{args.query}", result)
        return 0

    print(f"\n=== 检索：{args.query}（租户 {args.tenant_id}，模式 {args.mode}）===")
    print(f"候选 {result.get('candidates', 0)} 条，返回 {len(result.get('results', []))} 条")
    for i, r in enumerate(result.get("results", []), start=1):
        text = (r["text"] or "").replace("\n", " ")
        print(f"\n[{i}] score={r['score']:.4f} rrf={r['rrf_score']:.4f} 词面={r['lexical_score']:.3f}")
        print(f"    来源：{r['kb_type']} / {r['doc_id']} 第 {r['chunk_ix']} 片")
        print(f"    内容：{text[:160]}{'...' if len(text) > 160 else ''}")
    if result.get("warning"):
        print(f"\n⚠️  {result['warning']}")
    return 0


def cmd_store(args) -> int:
    from bdp.kb.store import build_store, default_store

    store = build_store(args.backend) if args.backend else default_store()
    _print("向量库状态", store.describe())
    return 0


def cmd_kb(args) -> int:
    from bdp.kb.ingest import ingest_knowledge
    from bdp.kb.store import default_store

    with session_scope() as session:
        store = default_store()
        stats = ingest_knowledge(session, store=store, tenant_id=args.tenant_id, rebuild=args.rebuild)
    _print("知识库入库", stats)
    return 0


def cmd_api(args) -> int:
    import uvicorn

    uvicorn.run("bdp.api.main:app", host=args.host, port=args.port, reload=args.reload, app_dir="src")
    return 0


def cmd_eval(args) -> int:
    from bdp.kb.evaluate import compare_chunking, run_eval

    with session_scope() as session:
        if args.chunking:
            result = compare_chunking(session, sample=args.sample, top_k=args.top_k)
            _print("切片策略对比", result)
            return 0
        result = run_eval(session, top_k=args.top_k, backend=args.backend, sample=args.sample)

    _print("检索效果评测（消融）", {
        "eval_set_size": result["eval_set_size"],
        "embedding_backend": result["embedding_backend"],
        "vector_backend": result["vector_backend"],
        "top_k": result["top_k"],
    })
    header = f"{'模式':<9}{'重排':<7}{'Recall@5':<11}{'MRR@5':<10}{'P50(ms)':<10}{'P95(ms)'}"
    print(header)
    print("-" * len(header))
    for m in result["matrix"]:
        print(
            f"{m['mode']:<9}{str(m['rerank']):<7}{m['recall_at_k']:<11}"
            f"{m['mrr_at_k']:<10}{m['p50_latency_ms']:<10}{m['p95_latency_ms']}"
        )
    _print("结论", {
        "纯稠密基线 Recall@5": result["baseline_dense_recall"],
        "最佳组合": result["best"],
        "相对提升_pct": result["relative_improvement_pct"],
    })
    return 0


def cmd_stats(args) -> int:
    tables = [
        "dim_tenant", "dim_shop", "dim_spu", "dim_sku", "map_platform_sku",
        "raw_order", "raw_refund", "raw_cs_session",
        "dwd_order", "dwd_refund", "dwd_cs_session",
        "dws_shop_day", "dws_tenant_day",
        "metric_def", "metric_result", "dq_result", "kb_document", "kb_chunk", "audit_log",
    ]
    with session_scope() as session:
        counts = {}
        for t in tables:
            try:
                counts[t] = int(session.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() or 0)
            except Exception:
                counts[t] = "n/a"
        dq = [
            {"rule_id": r[0], "table": r[1], "failed": r[2], "pass_rate": r[3]}
            for r in session.execute(
                text(
                    "SELECT rule_id, table_name, failed_rows, pass_rate FROM dq_result "
                    "WHERE run_id = (SELECT MAX(run_id) FROM dq_result) ORDER BY rule_id"
                )
            ).all()
        ]
    _print("各表行数", counts)
    _print("数据质量明细", dq)
    return 0


# ---------------------------------------------------------------------------
# 多 agent 编排
# ---------------------------------------------------------------------------


def _print_run_summary(run_id: str) -> int:
    from bdp.agents.state import StateStore

    store = StateStore()
    run = store.get_run(run_id)
    tasks = store.list_tasks(run_id)
    if run is None:
        print(f"运行不存在：{run_id}")
        return 1
    print(f"\n=== Agent 运行 {run_id} ===")
    print(f"DAG：{run.dag_id}    状态：{run.status}")
    header = f"{'任务':<28}{'Agent':<12}{'状态':<12}{'尝试':<6}{'耗时(秒)':<10}{'租户'}"
    print(header)
    print("-" * len(header))
    for t in tasks:
        duration = ""
        if t.started_at and t.finished_at:
            duration = f"{(t.finished_at - t.started_at).total_seconds():.1f}"
        print(
            f"{t.name:<28}{t.agent:<12}{t.status:<12}{t.attempt:<6}{duration:<10}"
            f"{t.tenant_scope or '-'}"
        )
        if t.error:
            print(f"    └─ {t.error}")
    _print("运行统计", run.stats)
    return 0 if run.status == "succeeded" else 1


def cmd_agent_run(args) -> int:
    from bdp.agents.dag import get_dag
    from bdp.agents.orchestrator import Orchestrator

    dag = get_dag(args.dag)
    if args.drop:
        print("⚠️  即将删除全部数据表并重建（仅用于本地环境）")
        init_db(drop=True)

    params: dict = {}
    if getattr(args, "days", None):
        params["days"] = args.days
    if getattr(args, "seed", None):
        params["seed"] = args.seed

    started = time.time()
    run_id = Orchestrator().execute(dag, trigger="cli", params=params)
    _print("总耗时", f"{round(time.time() - started, 2)} 秒")
    return _print_run_summary(run_id)


def cmd_agent_status(args) -> int:
    from bdp.agents.state import StateStore

    store = StateStore()
    if args.run_id:
        return _print_run_summary(args.run_id)
    runs = store.list_runs(limit=args.limit)
    if not runs:
        print("尚无运行记录，先执行：python -m bdp.cli agent run")
        return 0
    header = f"{'run_id':<44}{'DAG':<12}{'状态':<16}{'触发':<8}{'开始时间'}"
    print(header)
    print("-" * len(header))
    for r in runs:
        print(f"{r.run_id:<44}{r.dag_id:<12}{r.status:<16}{r.trigger:<8}{r.started_at}")
    print("\n查看明细：python -m bdp.cli agent status <run_id>")
    return 0


def cmd_agent_retry(args) -> int:
    from bdp.agents.dag import get_dag
    from bdp.agents.orchestrator import Orchestrator
    from bdp.agents.state import StateStore

    store = StateStore()
    task = store.get_task(args.task_id)
    if task is None:
        print(f"任务不存在：{args.task_id}")
        return 1
    run = store.get_run(task.run_id)
    if run is None:
        print(f"任务 {task.task_id} 所属运行不存在")
        return 1
    run_id = Orchestrator().resume(get_dag(run.dag_id), run.run_id, [task.task_id])
    return _print_run_summary(run_id)


def cmd_agent_ask(args) -> int:
    from bdp.agents.orchestrator import run_agent_inline
    from bdp.agents.spec import TenantScope

    if not args.tenant_id:
        print("业务问答必须指定租户：--tenant-id T001（知识与品牌强绑定）")
        return 1
    result = run_agent_inline(
        "insight", {"question": args.question},
        scope=TenantScope(tenant_id=args.tenant_id),
    )
    insight = result.artifacts.get("insight", {})
    print(insight.get("answer", ""))
    if insight.get("citations"):
        _print("引用", insight["citations"])
    if result.warning:
        print(f"\n⚠️  {result.warning}")
    return 0


def cmd_all(args) -> int:
    for fn in (cmd_init, cmd_mock, cmd_pipeline, cmd_metrics):
        rc = fn(args)
        if rc != 0:
            return rc
    return cmd_kb(args)


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bdp", description="多品牌电商数据中台 · 命令行工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="建表（--drop 先清空重建）")
    p.add_argument("--drop", action="store_true")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--seed", type=int, default=None)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("mock", help="生成模拟数据")
    p.add_argument("--days", type=int, default=None, help="数据天数，默认取 .env 的 BDP_MOCK_DAYS")
    p.add_argument("--seed", type=int, default=None)
    p.set_defaults(func=cmd_mock)

    p = sub.add_parser("pipeline", help="跑数仓分层（raw→dwd→dws）与数据质量校验")
    p.set_defaults(func=cmd_pipeline)

    p = sub.add_parser("metrics", help="写入指标字典并物化指标结果")
    p.set_defaults(func=cmd_metrics)

    p = sub.add_parser("kb", help="知识库切片、向量化并写入向量库")
    p.add_argument("--tenant-id", default=None, help="只处理指定租户")
    p.add_argument("--rebuild", action="store_true", help="重建 collection")
    p.set_defaults(func=cmd_kb)

    p = sub.add_parser("search", help="检索知识库")
    p.add_argument("query", type=str)
    p.add_argument("--tenant-id", default=None)
    p.add_argument("--kb-type", default=None, help="cs_faq | product | policy | sop，默认全部")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--mode", default="hybrid", choices=["dense", "sparse", "hybrid"])
    p.add_argument("--backend", default=None, help="milvus | local")
    p.add_argument("--json", action="store_true", help="输出完整 JSON（含评分明细）")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("store", help="查看向量库状态（集合、向量数、租户数）")
    p.add_argument("--backend", default=None, help="milvus | local")
    p.set_defaults(func=cmd_store)

    p = sub.add_parser("eval", help="检索效果评测（Recall@K）")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--backend", default=None)
    p.add_argument("--sample", type=int, default=200, help="评测集规模")
    p.add_argument("--chunking", action="store_true", help="改为对比切片策略（fixed/semantic/hierarchical）")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("api", help="启动 API 与看板")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_api)

    p = sub.add_parser("stats", help="查看各表行数与数据质量结果")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("agent", help="多 agent 编排：运行 DAG / 查看状态 / 重试")
    asub = p.add_subparsers(dest="agent_command", required=True)

    ap = asub.add_parser("run", help="运行 DAG（默认 nightly：接入→数仓→质量/知识库→指标）")
    ap.add_argument("--dag", default="nightly")
    ap.add_argument("--drop", action="store_true", help="运行前清空重建全部表")
    ap.add_argument("--days", type=int, default=None, help="覆盖模拟数据天数")
    ap.add_argument("--seed", type=int, default=None, help="覆盖模拟数据种子")
    ap.set_defaults(func=cmd_agent_run)

    ap = asub.add_parser("status", help="查看运行列表或某次运行的任务明细")
    ap.add_argument("run_id", nargs="?", default=None)
    ap.add_argument("--limit", type=int, default=10)
    ap.set_defaults(func=cmd_agent_status)

    ap = asub.add_parser("retry", help="复位并重跑失败/被跳过的任务（含其下游）")
    ap.add_argument("task_id")
    ap.set_defaults(func=cmd_agent_retry)

    ap = asub.add_parser("ask", help="业务问答（InsightAgent；无 LLM 时走降级路径）")
    ap.add_argument("question")
    ap.add_argument("--tenant-id", required=True)
    ap.set_defaults(func=cmd_agent_ask)

    p = sub.add_parser("all", help="一键跑通：init + mock + pipeline + metrics + kb")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--tenant-id", default=None)
    p.add_argument("--rebuild", action="store_true")
    p.add_argument("--drop", action="store_true")
    p.set_defaults(func=cmd_all)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
