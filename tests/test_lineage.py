"""数据血缘测试。

核心是"声明不腐化"的三方校验：
血缘声明的表名必须真实存在（models）、via 任务必须真实存在（DAG）、
指标级血缘与指标字典的推导必须与计算引擎一致。
改了 pipeline/DAG/字典而忘了同步血缘声明 → 这里直接红。
"""

from __future__ import annotations

import pytest

from bdp.pipeline.lineage import full_lineage, metric_lineage, table_lineage


@pytest.fixture(scope="module")
def lineage_graph(seeded):
    return table_lineage()


def test_table_lineage_shape(lineage_graph):
    g = lineage_graph
    assert len(g.nodes) >= 15
    assert len(g.edges) >= 20
    ids = {n["id"] for n in g.nodes}
    for e in g.edges:
        assert e["source"] in ids, f"边引用了未声明节点：{e['source']}"
        assert e["target"] in ids, f"边引用了未声明节点：{e['target']}"
        assert e["via"], "每条边必须标注经由任务"


def test_lineage_tables_all_exist(seeded):
    """血缘声明的表名必须真实存在于 models。"""
    from bdp.models import Base

    real = set(Base.metadata.tables)
    g = table_lineage()
    fake = [e["source"] for e in g.edges if e["source"] not in real and e["source"] not in ("向量索引", "指标查询 API")]
    fake += [e["target"] for e in g.edges if e["target"] not in real and e["target"] not in ("向量索引", "指标查询 API")]
    assert not fake, f"血缘引用了不存在的表：{sorted(set(fake))}"


def test_lineage_tasks_all_in_dag(seeded):
    """via 任务必须真实存在于 nightly DAG。"""
    from bdp.agents.dag import get_dag

    dag_tasks = {t.name for t in get_dag("nightly").tasks}
    g = table_lineage()
    bad = {e["via"] for e in g.edges if e["via"] not in dag_tasks and e["via"] != "api"}
    assert not bad, f"血缘经由了 DAG 中不存在的任务：{sorted(bad)}"


def test_lineage_covers_all_dwd_tasks(seeded):
    """DAG 中声明了写域的批处理任务都必须在血缘里出现（防新增任务漏登记）。

    ingest 是纯源头（只写不读），校验它产出的表都作为源节点存在。
    """
    from bdp.agents.dag import get_dag

    g = table_lineage()
    vias = {e["via"] for e in g.edges}
    node_ids = {n["id"] for n in g.nodes}
    for t in get_dag("nightly").tasks:
        if t.name == "ingest":
            assert {"raw_order", "dwd_order"} <= node_ids  # 源头表在图中
            continue
        if t.name == "metrics":  # metrics 是 fan-out，血缘统一记作 metrics
            continue
        assert t.name in vias, f"DAG 任务 {t.name} 未在血缘中登记任何数据流"


def test_lineage_layered_flow(lineage_graph):
    """分层走向正确：raw 只出不进（除 kb_document 外）；dws 的输入必须含 dwd。"""
    g = lineage_graph
    into_raw = [e for e in g.edges if e["target"].startswith("raw_")]
    assert not into_raw, f"raw 层不应有输入边：{into_raw}"
    dws_inputs = {e["source"] for e in g.edges if e["target"] == "dws_tenant_day"}
    assert {"dwd_order", "dwd_refund", "dwd_cs_session"} <= dws_inputs


def test_metric_lineage_matches_registry(seeded):
    """指标级血缘与指标字典一致：基础指标挂来源表，派生指标挂公式依赖。"""
    from bdp.db import session_scope
    from bdp.metrics.registry import list_metrics

    with session_scope() as s:
        g = metric_lineage(s)
        metrics = {m["metric_code"]: m for m in list_metrics(s)}

    assert set(metrics) <= {n["id"] for n in g.nodes}, "字典里的指标缺节点"
    by_code = {}
    for m in metrics.values():
        by_code.setdefault(m["metric_code"], []).append(m)
    # 每个指标恰好一个节点（最新版本）
    node_ids = [n["id"] for n in g.nodes if n["kind"] == "metric"]
    assert len(node_ids) == len(by_code)

    # GMV_PAID 双版本：血缘节点应能看到全部版本
    gmv = next(n for n in g.nodes if n["id"] == "GMV_PAID")
    assert sorted(gmv["all_versions"]) == ["v1.0", "v1.1"]

    # 派生指标依赖：REFUND_RATE = REFUND_AMOUNT / GMV_PAID
    formula_edges = {(e["source"], e["target"]) for e in g.edges if e["via"] == "formula"}
    assert ("REFUND_AMOUNT", "REFUND_RATE") in formula_edges
    assert ("GMV_PAID", "REFUND_RATE") in formula_edges

    # 基础指标的来源表边
    source_edges = {(e["source"], e["target"]) for e in g.edges if e["via"] == "source_table"}
    assert ("dwd_order", "GMV_ORDER") in source_edges
    assert ("dwd_cs_session", "CS_SATISFACTION_AVG") in source_edges


def test_full_lineage_merge(seeded):
    """合并视图 = 表级 ∪ 指标级，且不丢边。"""
    from bdp.db import session_scope

    with session_scope() as s:
        g = full_lineage(s)
    t = table_lineage()
    with session_scope() as s:
        m = metric_lineage(s)
    assert {n["id"] for n in t.nodes} <= {n["id"] for n in g.nodes}
    assert {n["id"] for n in m.nodes} <= {n["id"] for n in g.nodes}
    t_edges = {(e["source"], e["target"], e["via"]) for e in t.edges}
    g_edges = {(e["source"], e["target"], e["via"]) for e in g.edges}
    assert t_edges <= g_edges


def test_lineage_api_requires_admin_or_ops(client, tokens):
    """brand 账号不可看全平台血缘（含跨租户表名与指标结构）。"""
    res = client.get("/v1/admin/lineage", headers={"Authorization": f"Bearer {tokens['nova']}"})
    assert res.status_code == 403


def test_lineage_api_returns_sankey_shape(client, tokens):
    for username in ("admin", "ops"):
        res = client.get(
            "/v1/admin/lineage",
            headers={"Authorization": f"Bearer {tokens[username]}"},
        )
        assert res.status_code == 200, res.text
        d = res.json()
        assert {"nodes", "edges"} <= set(d)
        assert all({"source", "target", "via"} <= set(e) for e in d["edges"])
    for path in ("/v1/admin/lineage/tables", "/v1/admin/lineage/metrics"):
        res = client.get(path, headers={"Authorization": f"Bearer {tokens['admin']}"})
        assert res.status_code == 200
