"""数据血缘（Data Lineage）。

设计取舍
--------
血缘有两种做法：加工代码埋点上报（生产级平台的做法，需要 SDK 与后端存储），
或**声明式编译**（本项目做法）：把"哪个任务读了什么、写了什么"作为数据显式声明，
查询时编译成图，并用测试强制声明与 models/DAG/指标字典三方一致。

选声明式的理由：链路只有 8 个任务、26 张表，埋点上报的基础设施成本远大于收益；
声明 + 测试校验已经能保证血缘不会腐化（改了 pipeline 忘改声明，测试直接红）。
局限（诚实声明）：粒度是表级/指标级（非列级）；覆盖批处理与知识库链路；
不覆盖 API 的临时只读查询（只读不写，不产生新数据）。

指标级血缘则是**真·从数据推导**：基础指标按字典的 source_table、
派生指标按公式的指标代码引用（与 metrics.engine 的 _FORMULA_TOKEN 同一套
解析规则），指标字典怎么改，血缘就怎么变，无需额外维护。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# 表级血缘：精确声明的数据流（source → target，经由任务）
# ---------------------------------------------------------------------------
# 每条边都必须能在 pipeline 代码里指出真实的读写行为（code review 的义务）；
# tests/test_lineage.py 校验：表名真实存在、via 任务真实存在于 DAG、
# DAG 中每个有写域的任务都在边里出现。

_TABLE_EDGES: list[tuple[str, str, str]] = [
    # -- dwd：清洗 + 主数据映射（pipeline/dwd.py）
    ("raw_order", "dwd_order", "dwd_orders"),
    ("dim_sku", "dwd_order", "dwd_orders"),
    ("map_platform_sku", "dwd_order", "dwd_orders"),
    ("raw_refund", "dwd_refund", "dwd_refunds"),
    ("map_platform_sku", "dwd_refund", "dwd_refunds"),
    ("raw_cs_session", "dwd_cs_session", "dwd_cs"),
    # -- dws：轻度汇总，join dim_shop 取平台属性（pipeline/dws.py）
    ("dwd_order", "dws_shop_day", "dws"),
    ("dwd_order", "dws_tenant_day", "dws"),
    ("dwd_refund", "dws_shop_day", "dws"),
    ("dwd_refund", "dws_tenant_day", "dws"),
    ("dwd_cs_session", "dws_shop_day", "dws"),
    ("dwd_cs_session", "dws_tenant_day", "dws"),
    ("dim_shop", "dws_shop_day", "dws"),
    ("dim_shop", "dws_tenant_day", "dws"),
    # -- 治理：质量规则扫 raw 与 dwd（pipeline/quality.py）
    ("raw_order", "dq_result", "quality"),
    ("raw_refund", "dq_result", "quality"),
    ("dwd_order", "dq_result", "quality"),
    # -- 知识库：切片 → 向量化（kb/ingest.py，同一任务内两步）
    ("kb_document", "kb_chunk", "kb_rebuild"),
    ("kb_chunk", "向量索引", "kb_rebuild"),
    # -- ads：指标物化按字典对 dwd 明细聚合（metrics/engine.py）
    ("dwd_order", "metric_result", "metrics"),
    ("dwd_refund", "metric_result", "metrics"),
    ("dwd_cs_session", "metric_result", "metrics"),
    # -- 服务层：API 只读消费（routers/metrics.py dashboard/summary 与 query）
    ("dws_shop_day", "指标查询 API", "api"),
    ("dws_tenant_day", "指标查询 API", "api"),
    ("metric_result", "指标查询 API", "api"),
]

# 图的源头节点：由 ingest 任务产出、未被链路下游读取的参照数据
# （dim_tenant/dim_spu/app_user 仅被 API 与登录路径消费，不计入批处理血缘）。
_SOURCE_TABLES = ("raw_order", "raw_refund", "raw_cs_session",
                  "dim_sku", "map_platform_sku", "dim_shop", "kb_document")

_EXTERNAL_NODES = ("向量索引", "指标查询 API")

_LAYER = {
    "raw_order": "raw", "raw_refund": "raw", "raw_cs_session": "raw",
    "dim_sku": "dim", "map_platform_sku": "dim", "dim_shop": "dim",
    "dwd_order": "dwd", "dwd_refund": "dwd", "dwd_cs_session": "dwd",
    "dws_shop_day": "dws", "dws_tenant_day": "dws",
    "metric_def": "ads", "metric_result": "ads",
    "dq_result": "治理", "dq_rule": "治理",
    "kb_document": "知识", "kb_chunk": "知识",
}


@dataclass
class LineageGraph:
    """血缘图：节点带分层与类型（table/metric/external），边带经由任务（via）。"""

    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)

    def add_node(self, node_id: str, kind: str, layer: str, **extra) -> None:
        if any(n["id"] == node_id for n in self.nodes):
            return
        n = {"id": node_id, "kind": kind, "layer": _LAYER.get(node_id, layer)}
        n.update(extra)
        self.nodes.append(n)

    def add_edge(self, source: str, target: str, via: str = "") -> None:
        for e in self.edges:
            if e["source"] == source and e["target"] == target:
                return
        self.edges.append({"source": source, "target": target, "via": via})


def table_lineage() -> LineageGraph:
    g = LineageGraph()
    for t in _SOURCE_TABLES:
        g.add_node(t, "table", "other")
    for src, dst, via in _TABLE_EDGES:
        kind = "external" if dst in _EXTERNAL_NODES else "table"
        g.add_node(src, "table", "other")
        g.add_node(dst, kind, "other")
        g.add_edge(src, dst, via=via)
    return g


_METRIC_TOKEN = re.compile(r"[A-Z][A-Z0-9_]*")


def metric_lineage(session: Session) -> LineageGraph:
    """指标级血缘：来源表 → 基础指标 → 派生指标。

    每个指标取生效日期最新的口径版本（血缘关注"现在怎么算"，
    历史版本差异在 /v1/metrics/{code}/versions 里看）；
    derived 公式的依赖解析规则与 metrics.engine 保持一致。
    """
    from bdp.models import MetricDef

    rows = session.execute(select(MetricDef).where(MetricDef.status == 1)).scalars().all()
    latest: dict[str, MetricDef] = {}
    for r in rows:
        cur = latest.get(r.metric_code)
        if cur is None or (r.effective_from, r.caliber_version) > (cur.effective_from, cur.caliber_version):
            latest[r.metric_code] = r

    g = LineageGraph()
    for code, r in latest.items():
        versions = sorted(v.caliber_version for v in rows if v.metric_code == code)
        g.add_node(
            code, "metric", "ads",
            label=r.metric_name, version=r.caliber_version, all_versions=versions,
            owner=r.owner, unit=r.unit,
        )

    for code, r in latest.items():
        if r.source_table == "derived":
            for dep in sorted(set(_METRIC_TOKEN.findall(r.agg_expr))):
                if dep in latest:
                    g.add_edge(dep, code, via="formula")
        else:
            g.add_node(r.source_table, "table", "other")
            g.add_edge(r.source_table, code, via="source_table")
    return g


def full_lineage(session: Session) -> LineageGraph:
    """合并表级 + 指标级：raw → dwd → dws/指标 → 派生指标 → 服务层的完整链路。"""
    t = table_lineage()
    m = metric_lineage(session)
    g = LineageGraph(nodes=list(t.nodes), edges=list(t.edges))
    for n in m.nodes:
        g.add_node(n["id"], n["kind"], n.get("layer", "other"), **{
            k: v for k, v in n.items() if k not in ("id", "kind", "layer")
        })
    for e in m.edges:
        g.add_edge(e["source"], e["target"], via=e["via"])
    return g


def to_dict(g: LineageGraph) -> dict:
    """序列化为前端可直接消费的结构（ECharts sankey 只需 nodes/links）。"""
    return {"nodes": g.nodes, "edges": g.edges}
