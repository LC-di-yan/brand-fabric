"""主数据映射测试：归一化、三级匹配、冲突检测。

跨平台商品映射是代运营场景里最脏的一块数据，
归一化漏一个分支就会导致大批订单无法归因到商品维度。
"""

from __future__ import annotations

from rapidfuzz import fuzz

from bdp.db import session_scope
from bdp.pipeline.mdm import FUZZY_CUTOFF, PlatformSkuMatcher
from bdp.pipeline.normalize import (
    extract_numeric,
    normalize_barcode,
    normalize_code,
    normalize_text,
)

from .conftest import auth


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def test_normalize_code_handles_case_space_and_fullwidth():
    assert normalize_code("  tmall-0000123  ") == "TMALL-0000123"
    assert normalize_code("TMALL－0000123") == "TMALL-0000123"  # 全角连字符
    assert normalize_code("ｔｍａｌｌ－１") == "TMALL-1"          # 全角字母数字
    assert normalize_code(None) == ""
    assert normalize_code("TM ALL-1") == "TMALL-1"


def test_normalize_barcode_strips_punctuation():
    assert normalize_barcode("BC-0001 23") == "BC000123"
    assert normalize_barcode(None) == ""


def test_normalize_text_keeps_case_but_compresses_space():
    assert normalize_text("  NOVA   退货政策  ") == "NOVA 退货政策"


def test_extract_numeric_tolerates_dirty_values():
    assert extract_numeric(None) is None
    assert extract_numeric("") is None
    assert extract_numeric("--") is None
    assert extract_numeric("123.45") == 123.45
    assert extract_numeric(12) == 12.0


# ---------------------------------------------------------------------------
# 匹配器
# ---------------------------------------------------------------------------


def test_matcher_index_and_exact_hit(seeded):
    with session_scope() as session:
        matcher = PlatformSkuMatcher.build(session)
        assert matcher.exact_index_size > 0

        # 取一条真实的映射记录，验证大小写/空格变体仍能命中
        from sqlalchemy import select

        from bdp.models import PlatformSkuMap

        row = session.execute(
            select(PlatformSkuMap).limit(1)
        ).scalar_one()
        res = matcher.match(row.tenant_id, row.platform, row.shop_id, row.platform_sku_code)
        assert res.match_type == "exact"
        assert res.sku_id == row.sku_id

        variant = f"  {row.platform_sku_code.lower()} "
        res2 = matcher.match(row.tenant_id, row.platform, row.shop_id, variant)
        assert res2.sku_id == row.sku_id, "归一化后的大小写/空格变体必须命中同一条主数据"


def test_matcher_falls_back_to_fuzzy_for_unfixable_codes(seeded):
    """缺位编码归一化修不了，必须由模糊匹配兜底。"""
    with session_scope() as session:
        matcher = PlatformSkuMatcher.build(session)
        from sqlalchemy import select

        from bdp.models import PlatformSkuMap

        row = session.execute(select(PlatformSkuMap).limit(1)).scalar_one()
        broken = row.platform_sku_code.replace("0", "", 1)
        if normalize_code(broken) == normalize_code(row.platform_sku_code):
            return  # 该编码不含 0，跳过（不影响其他用例）

        assert fuzz.ratio(normalize_code(broken), normalize_code(row.platform_sku_code)) >= FUZZY_CUTOFF
        res = matcher.match(row.tenant_id, row.platform, row.shop_id, broken)
        assert res.sku_id == row.sku_id
        assert res.match_type in ("fuzzy", "exact")


def test_matcher_returns_none_for_unknown_tenant(seeded):
    with session_scope() as session:
        matcher = PlatformSkuMatcher.build(session)
        res = matcher.match("T999", "tmall", "S999", "NOT-EXIST-0001")
        assert res.sku_id is None and res.match_type == "none"


def test_orders_are_mostly_mapped(seeded):
    """整体映射率是主数据质量的硬指标。"""
    from sqlalchemy import func, select

    from bdp.models import DwdOrder

    with session_scope() as session:
        total = session.execute(select(func.count()).select_from(DwdOrder)).scalar_one()
        mapped = session.execute(
            select(func.count()).select_from(
                select(DwdOrder).where(DwdOrder.sku_id.is_not(None)).subquery()
            )
        ).scalar_one()
        fuzzy = session.execute(
            select(func.count()).select_from(
                select(DwdOrder).where(DwdOrder.map_confidence < 0.999).subquery()
            )
        ).scalar_one()
    assert total > 0
    assert mapped / total >= 0.99, f"映射率过低：{mapped}/{total}"
    assert fuzzy > 0, "应当存在少量依赖模糊匹配的记录（模拟数据已注入）"


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


def test_api_mapping_coverage(client, tokens):
    res = client.get("/v1/catalog/mapping/coverage", headers=auth(tokens["nova"]))
    assert res.status_code == 200
    body = res.json()
    assert body["match_rate"] >= 0.99
    assert body["by_platform"], "应给出分平台映射情况"
    assert all(p["platform"] for p in body["by_platform"])


def test_api_products_are_scoped_to_tenant(client, tokens):
    res = client.get("/v1/catalog/products?limit=5", headers=auth(tokens["nova"]))
    assert res.status_code == 200
    body = res.json()
    assert body["items"]
    assert all(i["tenant_id"] == "T001" for i in body["items"])
    assert all(i["platform_mapping_count"] >= 0 for i in body["items"])
