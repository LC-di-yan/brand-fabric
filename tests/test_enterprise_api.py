"""企业级 API 能力测试：统一错误、指标对比与导出、主数据复核、知识库管理、数据质量样本、自检。"""

from __future__ import annotations

import pytest

from .conftest import auth


# ---------------------------------------------------------------------------
# 统一错误格式与请求 ID
# ---------------------------------------------------------------------------


def test_error_response_has_unified_format(client, tokens):
    res = client.get("/v1/dashboard/summary", headers=auth(tokens["nova"], "T002"))
    assert res.status_code == 403
    body = res.json()
    assert body["code"] == "TENANT_VIOLATION"
    assert body["message"]
    assert body["request_id"]
    # 响应头也带 request_id，方便排查
    assert res.headers.get("X-Request-Id")


def test_validation_error_is_structured(client, tokens):
    res = client.post(
        "/v1/metrics/query",
        headers=auth(tokens["nova"]),
        json={"metric_code": "GMV_PAID"},  # 缺 start/end
    )
    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["issues"]


def test_prometheus_metrics_endpoint(client, tokens):
    res = client.get("/metrics")
    assert res.status_code == 200
    text = res.text
    assert "bdp_http_requests_total" in text
    assert "bdp_http_request_duration_seconds_bucket" in text


# ---------------------------------------------------------------------------
# 指标：同比 / 环比 / 导出
# ---------------------------------------------------------------------------


def test_metric_mom_compare_returns_delta(client, tokens, seeded):
    """环比应当返回上一周期的总量与差值。

    窗口选在测试数据覆盖范围内（TEST_DAYS 生成的最近 2 天），
    保证上一周期同样有数据，避免"对比期为空"的干扰。
    """
    res = client.post(
        "/v1/metrics/query",
        headers=auth(tokens["nova"]),
        json={
            "metric_code": "GMV_PAID", "compare": "mom",
            "start": "2026-09-24", "end": "2026-09-25",
        },
    )
    assert res.status_code == 200
    compare = res.json()["compare"]
    assert compare["type"] == "mom"
    assert compare["previous_total"] is not None
    assert compare["delta"] is not None
    assert compare["delta_pct"] is not None


def test_metric_yoy_compare_gracefully_reports_no_data(client, tokens):
    """同比到上年没有数据时，应当优雅地报告"对比期无数据"而不是 500。"""
    res = client.post(
        "/v1/metrics/query",
        headers=auth(tokens["nova"]),
        json={"metric_code": "GMV_PAID", "compare": "yoy",
              "start": "2026-09-01", "end": "2026-09-25"},
    )
    assert res.status_code == 200
    compare = res.json()["compare"]
    assert compare["previous_total"] is None
    assert "无可用数据" in compare["note"]


def test_metric_export_csv_and_cross_tenant_blocked(client, tokens):
    res = client.get(
        "/v1/metrics/export?metric_code=GMV_PAID&start=2026-09-20&end=2026-09-25",
        headers=auth(tokens["nova"]),
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    lines = [ln for ln in res.text.strip().splitlines() if ln]
    assert lines[0].startswith("dt,tenant_id")
    assert len(lines) > 1

    denied = client.get(
        "/v1/metrics/export?metric_code=GMV_PAID&start=2026-09-01&end=2026-09-25",
        headers=auth(tokens["nova"], "T002"),
    )
    assert denied.status_code == 403


# ---------------------------------------------------------------------------
# 主数据复核
# ---------------------------------------------------------------------------


def test_mapping_review_queue_and_verify(client, tokens):
    res = client.get("/v1/catalog/mapping/review", headers=auth(tokens["nova"]))
    assert res.status_code == 200
    body = res.json()
    if not body["items"]:
        pytest.skip("当前数据没有待复核记录")

    item = body["items"][0]
    assert item["candidates"], "复核队列应给出候选"
    candidate = item["candidates"][0]

    verify = client.post(
        "/v1/catalog/mapping/verify",
        headers=auth(tokens["nova"]),
        json={
            "platform": item["platform"], "shop_id": item["shop_id"],
            "platform_sku_code": item["platform_sku_code"], "sku_id": candidate["sku_id"],
        },
    )
    assert verify.status_code == 200
    assert verify.json()["verified"] is True


def test_mapping_verify_rejects_wrong_tenant_sku(client, tokens):
    # nova 试图把编码指到 aurora 的 SKU 上 —— 必须被拒
    res = client.post(
        "/v1/catalog/mapping/verify",
        headers=auth(tokens["nova"]),
        json={
            "platform": "tmall", "shop_id": "S10101",
            "platform_sku_code": "FAKE-0001", "sku_id": "T002-SPU0001-SKU01",
        },
    )
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# 知识库管理
# ---------------------------------------------------------------------------


def test_kb_document_crud_flow(client, tokens):
    # 新增
    create = client.post(
        "/v1/kb/documents",
        headers=auth(tokens["nova"]),
        json={"kb_type": "cs_faq", "source_id": "MANUAL",
              "title": "测试补充规则", "content": "测试内容：大件商品退货需保留原包装。"},
    )
    assert create.status_code == 200
    doc_id = create.json()["doc_id"]

    # 检索能找到
    search = client.post(
        "/v1/kb/search",
        headers=auth(tokens["nova"], "T001"),
        json={"query": "大件商品退货要注意什么", "top_k": 3},
    )
    assert search.status_code == 200
    assert any(r["doc_id"] == doc_id for r in search.json()["results"])

    # 更新后切片重建
    update = client.put(
        f"/v1/kb/documents/{doc_id}",
        headers=auth(tokens["nova"]),
        json={"kb_type": "cs_faq", "source_id": "MANUAL",
              "title": "测试补充规则(更新)", "content": "更新内容：大件商品退货需保留原包装与配件。"},
    )
    assert update.status_code == 200

    # 删除后检索不再命中该文档
    delete = client.delete(f"/v1/kb/documents/{doc_id}", headers=auth(tokens["nova"]))
    assert delete.status_code == 200
    search2 = client.post(
        "/v1/kb/search",
        headers=auth(tokens["nova"], "T001"),
        json={"query": "大件商品退货要注意什么", "top_k": 5},
    )
    assert not any(r["doc_id"] == doc_id for r in search2.json()["results"])


def test_kb_create_validation(client, tokens):
    res = client.post(
        "/v1/kb/documents",
        headers=auth(tokens["nova"]),
        json={"kb_type": "invalid", "source_id": "MANUAL", "title": "x", "content": "y"},
    )
    assert res.status_code == 400

    res = client.post(
        "/v1/kb/documents",
        headers=auth(tokens["nova"]),
        json={"kb_type": "cs_faq", "source_id": "MANUAL", "title": "", "content": "y"},
    )
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# 数据质量样本 / 自检
# ---------------------------------------------------------------------------


def test_data_quality_samples(client, tokens):
    res = client.get("/v1/admin/data-quality/DQ004/samples", headers=auth(tokens["nova"]))
    assert res.status_code == 200
    body = res.json()
    assert body["rule_id"] == "DQ004"
    assert body["samples"], "应当有失败样本"
    # brand 账号只能看到自己租户的样本
    assert all(s.get("tenant_id") in ("T001", None) for s in body["samples"])

    missing = client.get("/v1/admin/data-quality/NOPE/samples", headers=auth(tokens["nova"]))
    assert missing.status_code == 404


def test_selfcheck_endpoint(client, tokens):
    res = client.get("/v1/admin/selfcheck", headers=auth(tokens["nova"]))
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    names = {i["name"] for i in body["items"]}
    assert {"database", "vector_store", "embedding", "security_config"} <= names
