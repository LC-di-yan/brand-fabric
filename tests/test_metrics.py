"""指标与口径治理测试。

覆盖两件容易出错的事：
    ① 口径版本解析（历史报表必须用历史口径）
    ② 不可加指标的周期总计（比率、均值不能把每日值相加）
"""

from __future__ import annotations

from datetime import date

import pytest

from bdp.db import session_scope
from bdp.metrics import registry
from bdp.metrics.engine import MetricError, compute

from .conftest import auth


def test_resolve_version_picks_latest_effective(seeded):
    """GMV_PAID 在 8 月底之前用 v1.0（含预售），9 月起切到 v1.1（剔除预售）。"""
    with session_scope() as session:
        assert registry.resolve_version(session, "GMV_PAID", date(2026, 8, 15)).caliber_version == "v1.0"
        assert registry.resolve_version(session, "GMV_PAID", date(2026, 9, 15)).caliber_version == "v1.1"
        assert registry.resolve_version(session, "不存在的指标", date(2026, 9, 15)) is None


def test_same_metric_two_calibers_produce_different_numbers(seeded):
    with session_scope() as session:
        v10 = compute(session, "GMV_PAID", caliber_version="v1.0",
                      tenant_id="T001", start=seeded["start"], end=seeded["end"])
        v11 = compute(session, "GMV_PAID", caliber_version="v1.1",
                      tenant_id="T001", start=seeded["start"], end=seeded["end"])
    assert v10["caliber_version"] == "v1.0"
    assert v11["caliber_version"] == "v1.1"
    # v1.1 剔除预售，金额必然不大于 v1.0
    assert v11["total"] <= v10["total"]


def test_additive_metric_total_equals_sum_of_days(seeded):
    with session_scope() as session:
        res = compute(session, "GMV_ORDER", tenant_id="T001", start=seeded["start"], end=seeded["end"])
    assert abs(res["total"] - res["additive_total"]) < 1.0, "可加指标的周期总计应等于日值之和"


def test_ratio_metric_total_is_not_sum_of_days(seeded):
    """退款率不可加：周期总计必须回到明细重算，而不是把每日比率相加。"""
    with session_scope() as session:
        res = compute(session, "REFUND_RATE", tenant_id="T001", start=seeded["start"], end=seeded["end"])
    assert res["total"] is not None
    assert 0 <= res["total"] <= 1, f"退款率应落在 0~1，实际 {res['total']}"
    assert res["additive_total"] > res["total"], "把每日比率相加会得到明显偏大的错误值"


def test_average_metric_stays_in_valid_range(seeded):
    with session_scope() as session:
        res = compute(session, "CS_SATISFACTION_AVG", tenant_id="T001",
                      start=seeded["start"], end=seeded["end"])
    assert 1.0 <= res["total"] <= 5.0, f"满意度均值应在 1~5，实际 {res['total']}"


def test_derived_metric_formula_is_reported(seeded):
    with session_scope() as session:
        res = compute(session, "AOV_PAID", tenant_id="T001", start=seeded["start"], end=seeded["end"])
    assert res["formula"] == "GMV_PAID / PAID_CNT"
    assert res["total"] > 0


def test_shop_dimension_returns_per_shop_points(seeded):
    with session_scope() as session:
        res = compute(session, "GMV_PAID", tenant_id="T001", dim_type="shop",
                      start=seeded["start"], end=seeded["end"])
    dim_values = {p["dim_value"] for p in res["points"]}
    assert len(dim_values) >= 2, "NOVA 至少有 2 个店铺"
    assert all(p["dim_value"].startswith("S") for p in res["points"])


def test_unknown_metric_and_bad_range_raise(seeded):
    with session_scope() as session:
        with pytest.raises(MetricError):
            compute(session, "NOT_A_METRIC", tenant_id="T001",
                    start=seeded["start"], end=seeded["end"])
        with pytest.raises(MetricError):
            compute(session, "GMV_PAID", tenant_id="T001",
                    start=seeded["end"], end=seeded["start"])
        with pytest.raises(MetricError):
            compute(session, "GMV_PAID", caliber_version="v9.9", tenant_id="T001",
                    start=seeded["start"], end=seeded["end"])


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


def test_api_metric_query_returns_caliber_notice(client, tokens, seeded):
    res = client.post(
        "/v1/metrics/query",
        headers=auth(tokens["nova"]),
        json={
            "metric_code": "GMV_PAID",
            "start": seeded["start"].isoformat(),
            "end": seeded["end"].isoformat(),
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["caliber_version"] in ("v1.0", "v1.1")
    assert body["caliber_notice"], "报表必须回传口径说明，否则历史数据会被静默改写"
    assert "口径" in body["caliber_notice"]


def test_api_metric_versions_endpoint(client, tokens):
    res = client.get("/v1/metrics/GMV_PAID/versions", headers=auth(tokens["nova"]))
    assert res.status_code == 200
    versions = [v["caliber_version"] for v in res.json()["versions"]]
    assert versions == ["v1.0", "v1.1"]

    missing = client.get("/v1/metrics/NOPE/versions", headers=auth(tokens["nova"]))
    assert missing.status_code == 404


def test_api_metric_list_exposes_owner_and_effective_window(client, tokens):
    res = client.get("/v1/metrics", headers=auth(tokens["nova"]))
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) >= 15
    sample = next(i for i in items if i["metric_code"] == "GMV_SETTLE")
    assert sample["owner"]
    assert sample["effective_from"]
    assert sample["is_derived"] is True
