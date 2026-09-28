"""主数据生成：租户/店铺/SPU/SKU/跨平台编码映射。

从 generator 拆出。platform_sku_map 是"跨平台商品主数据"问题的源头：
订单里带的是平台侧编码，需要管道通过归一化 + 模糊匹配还原。
"""

from __future__ import annotations

import random

from sqlalchemy.orm import Session

from bdp.mock.brands import BRANDS, _dirty_sku_code
from bdp.models import PlatformSkuMap, Shop, Sku, Spu, Tenant

def _gen_master_data(session: Session, rng: random.Random) -> tuple[list[dict], list[dict]]:
    tenants, shops = [], []
    for spec in BRANDS:
        tenants.append(
            dict(
                tenant_id=spec.tenant_id,
                name=spec.name,
                category_l1=spec.category_l1,
                category_l2=spec.category_l2,
                status=1,
            )
        )
        for idx, (platform, shop_name) in enumerate(spec.shops, start=1):
            shops.append(
                dict(
                    shop_id=f"S{spec.tenant_id[1:]}{idx:02d}",
                    tenant_id=spec.tenant_id,
                    platform=platform,
                    shop_name=shop_name,
                    shop_type="flagship",
                    status=1,
                )
            )
    session.bulk_insert_mappings(Tenant, tenants)
    session.bulk_insert_mappings(Shop, shops)
    session.flush()
    return tenants, shops


def _gen_products(session: Session, rng: random.Random) -> tuple[list[dict], list[dict]]:
    spus, skus = [], []
    for spec in BRANDS:
        for i in range(1, spec.spu_count + 1):
            spu_id = f"{spec.tenant_id}-SPU{i:04d}"
            word = rng.choice(spec.product_words)
            seq = rng.choice(["PRO", "MAX", "AIR", "LITE", "PLUS"])
            spus.append(
                dict(
                    spu_id=spu_id,
                    tenant_id=spec.tenant_id,
                    spu_name=f"{spec.name} {word}{spec.category_l2}款 {seq}-{i:03d}",
                    category_l1=spec.category_l1,
                    category_l2=spec.category_l2,
                    brand_line=f"{spec.name}主品牌",
                )
            )
            list_price = round(rng.uniform(*spec.price_range), 2)
            for j, spec_name in enumerate(_sku_specs(spec.category_l1)[: rng.randint(2, 4)], start=1):
                skus.append(
                    dict(
                        sku_id=f"{spu_id}-SKU{j:02d}",
                        spu_id=spu_id,
                        tenant_id=spec.tenant_id,
                        # 集团统一条码：存在少量大小写/空格不规范，用于验证清洗
                        barcode=_dirty_sku_code(rng, f"BC{spec.tenant_id[1:]}{i:04d}{j:02d}".upper(), hard=False),
                        spec=spec_name,
                        list_price=round(list_price * rng.uniform(0.85, 1.15), 2),
                        status=1,
                    )
                )
    session.bulk_insert_mappings(Spu, spus)
    session.bulk_insert_mappings(Sku, skus)
    session.flush()
    return spus, skus


def _sku_specs(category_l1: str) -> list[str]:
    return {
        "服饰": ["S码/黑色", "M码/黑色", "L码/黑色", "XL码/白色", "M码/藏青"],
        "美妆": ["30ml装", "50ml装", "礼盒装", "试用装"],
        "3C数码": ["标准版", "Pro版", "尊享版", "套装版"],
        "家居": ["1.5m床", "1.8m床", "单人款", "双人款"],
    }.get(category_l1, ["标准款"])


def _gen_platform_sku_map(
    session: Session, rng: random.Random, skus: list[dict], shops: list[dict]
) -> dict[tuple[str, str, str, str], str]:
    """为每个 SKU 在其品牌各平台的店铺生成平台侧编码。

    这是"跨平台商品主数据"问题的源头：同一个内部 SKU 在不同平台/店铺
    有完全不同的编码，且上游编码不规范。管道需要通过归一化 + 模糊匹配还原对应关系。

    返回值是 (tenant_id, platform, shop_id, sku_id) -> 平台侧原始编码 的查找表，
    供订单生成使用 —— 这样订单里带的才是"平台编码"而不是内部条码，
    与实际业务一致（订单是平台产生的，平台不认识你的内部编码）。
    """
    shops_by_tenant: dict[str, list[dict]] = {}
    for s in shops:
        shops_by_tenant.setdefault(s["tenant_id"], []).append(s)

    mappings: list[dict] = []
    lookup: dict[tuple[str, str, str, str], str] = {}

    for sku in skus:
        for shop in shops_by_tenant.get(sku["tenant_id"], []):
            seq = len(mappings) + 1
            clean_code = f"{shop['platform'].upper()}-{seq:07d}"
            dirty = _dirty_sku_code(rng, clean_code, hard=True)
            mappings.append(
                dict(
                    tenant_id=sku["tenant_id"],
                    sku_id=sku["sku_id"],
                    platform=shop["platform"],
                    shop_id=shop["shop_id"],
                    platform_item_id=f"ITM{seq:08d}",
                    platform_sku_code=dirty,
                    match_type="rule",
                    confidence=1.0,
                    verified=False,
                )
            )
            lookup[(sku["tenant_id"], shop["platform"], shop["shop_id"], sku["sku_id"])] = dirty

    session.bulk_insert_mappings(PlatformSkuMap, mappings)
    session.flush()
    return lookup
