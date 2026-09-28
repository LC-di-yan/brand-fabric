"""主数据服务：店铺、商品、跨平台映射覆盖率。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Integer, desc, func, select
from sqlalchemy.orm import Session

from bdp.api.deps import get_principal, tenant_guard
from bdp.api.schemas import MappingVerify
from bdp.db import get_db
from bdp.models import DwdOrder, PlatformSkuMap, RawOrder, Shop, Sku, Spu, Tenant
from bdp.pipeline.mdm import PlatformSkuMatcher
from bdp.security.auth import Principal

router = APIRouter(tags=["catalog"])


@router.get("/v1/catalog/tenants", summary="租户（品牌）列表")
def list_tenants(
    effective_tenant: str | None = Depends(tenant_guard("catalog.tenants", "dim_tenant")),
    session: Session = Depends(get_db),
) -> dict:
    stmt = select(Tenant).order_by(Tenant.tenant_id)
    if effective_tenant:
        stmt = stmt.where(Tenant.tenant_id == effective_tenant)
    rows = session.execute(stmt).scalars().all()
    return {
        "count": len(rows),
        "items": [
            {"tenant_id": r.tenant_id, "name": r.name, "category_l1": r.category_l1,
             "category_l2": r.category_l2}
            for r in rows
        ],
    }


@router.get("/v1/catalog/shops", summary="店铺列表")
def list_shops(
    effective_tenant: str | None = Depends(tenant_guard("catalog.shops", "dim_shop")),
    session: Session = Depends(get_db),
) -> dict:
    stmt = select(Shop).order_by(Shop.tenant_id, Shop.shop_id)
    if effective_tenant:
        stmt = stmt.where(Shop.tenant_id == effective_tenant)
    rows = session.execute(stmt).scalars().all()
    return {
        "count": len(rows),
        "items": [
            {"shop_id": r.shop_id, "tenant_id": r.tenant_id, "platform": r.platform,
             "shop_name": r.shop_name, "shop_type": r.shop_type}
            for r in rows
        ],
    }


@router.get("/v1/catalog/products", summary="商品主数据（SPU/SKU 与跨平台映射）")
def list_products(
    effective_tenant: str | None = Depends(tenant_guard("catalog.products", "dim_sku")),
    session: Session = Depends(get_db),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
    keyword: str | None = None,
) -> dict:
    stmt = select(Sku, Spu.spu_name).join(Spu, Sku.spu_id == Spu.spu_id)
    if effective_tenant:
        stmt = stmt.where(Sku.tenant_id == effective_tenant)
    if keyword:
        stmt = stmt.where(Spu.spu_name.like(f"%{keyword}%"))

    total = session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()

    rows = session.execute(stmt.order_by(Sku.sku_id).limit(limit).offset(offset)).all()
    sku_ids = [sku.sku_id for sku, _ in rows]

    mapping_counts: dict[str, int] = {}
    if sku_ids:
        mapping_counts = {
            sku_id: cnt
            for sku_id, cnt in session.execute(
                select(PlatformSkuMap.sku_id, func.count())
                .where(PlatformSkuMap.sku_id.in_(sku_ids))
                .group_by(PlatformSkuMap.sku_id)
            ).all()
        }

    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "sku_id": sku.sku_id,
                "spu_id": sku.spu_id,
                "spu_name": spu_name,
                "tenant_id": sku.tenant_id,
                "barcode": sku.barcode,
                "spec": sku.spec,
                "list_price": sku.list_price,
                "platform_mapping_count": mapping_counts.get(sku.sku_id, 0),
            }
            for sku, spu_name in rows
        ],
    }


@router.get("/v1/catalog/mapping/coverage", summary="跨平台映射覆盖率与匹配方式分布")
def mapping_coverage(
    effective_tenant: str | None = Depends(tenant_guard("catalog.mapping", "map_platform_sku")),
    session: Session = Depends(get_db),
) -> dict:
    filters = [DwdOrder.tenant_id == effective_tenant] if effective_tenant else []
    mapped_expr = func.cast(DwdOrder.sku_id.is_not(None), Integer)

    total = session.execute(
        select(func.count()).select_from(select(DwdOrder).where(*filters).subquery())
    ).scalar_one()
    matched = session.execute(
        select(func.count()).select_from(
            select(DwdOrder).where(*filters, DwdOrder.sku_id.is_not(None)).subquery()
        )
    ).scalar_one()
    fuzzy = session.execute(
        select(func.count()).select_from(
            select(DwdOrder).where(*filters, DwdOrder.map_confidence < 0.999).subquery()
        )
    ).scalar_one()

    platform_rows = session.execute(
        select(DwdOrder.platform, func.count(), func.sum(mapped_expr))
        .where(*filters)
        .group_by(DwdOrder.platform)
    ).all()

    return {
        "tenant_id": effective_tenant or "ALL",
        "orders": int(total),
        "mapped": int(matched),
        "match_rate": round(int(matched) / int(total), 4) if total else 0.0,
        "fuzzy_matched": int(fuzzy),
        "fuzzy_share": round(int(fuzzy) / int(total), 4) if total else 0.0,
        "mapping_rows": int(
            session.execute(select(func.count()).select_from(PlatformSkuMap)).scalar_one()
        ),
        "by_platform": [
            {
                "platform": p,
                "orders": int(t),
                "mapped": int(m or 0),
                "match_rate": round(int(m or 0) / int(t), 4) if t else 0.0,
            }
            for p, t, m in platform_rows
        ],
        "note": "模糊匹配占比反映上游编码不规范的程度；这部分记录归一化修不了，靠编辑距离兜底。",
    }


# ---------------------------------------------------------------------------
# 主数据复核：中台闭环里"机器处理不了、必须人来决定"的那部分
# ---------------------------------------------------------------------------


@router.get("/v1/catalog/mapping/review", summary="未匹配/低置信映射复核队列")
def mapping_review(
    effective_tenant: str | None = Depends(tenant_guard("catalog.mapping.review", "dwd_order")),
    session: Session = Depends(get_db),
    limit: int = Query(default=30, le=200),
) -> dict:
    """把"机器匹配不上或匹配可信度低"的记录列出来，并给出模糊候选，供人工确认。

    这部分记录是商品主数据治理里**唯一必须人工介入**的环节 —— 机器只能给候选，
    不能替人决定，否则错误映射会以 100% 置信度污染所有商品维度分析。
    """
    # 低置信（< 0.95）的记录，按平台编码聚合去重。
    # 平台原始编码在 raw 层，明细层只存映射结果，需要回联拿到原始编码。
    cond = [DwdOrder.map_confidence < 0.95]
    if effective_tenant:
        cond.append(DwdOrder.tenant_id == effective_tenant)

    stmt = (
        select(
            DwdOrder.tenant_id, DwdOrder.platform, DwdOrder.shop_id,
            RawOrder.platform_sku_code, func.count().label("cnt"),
            func.max(DwdOrder.map_confidence).label("conf"),
        )
        .join(RawOrder, DwdOrder.order_line_id == RawOrder.order_line_id)
        .where(*cond)
        .group_by(DwdOrder.tenant_id, DwdOrder.platform, DwdOrder.shop_id, RawOrder.platform_sku_code)
        .order_by(desc(func.count()))
        .limit(limit)
    )
    rows = session.execute(stmt).all()

    matcher = PlatformSkuMatcher.build(session)
    items = []
    for tenant_id, platform, shop_id, raw_code, cnt, conf in rows:
        items.append(
            {
                "tenant_id": tenant_id, "platform": platform, "shop_id": shop_id,
                "platform_sku_code": raw_code, "orders": int(cnt),
                "confidence": round(float(conf or 0.0), 3),
                "candidates": matcher.candidates(tenant_id, platform, raw_code, n=3),
            }
        )
    return {
        "tenant_id": effective_tenant or "ALL",
        "count": len(items),
        "items": items,
        "note": "confidence < 0.95 或无法映射的记录；人工确认后通过 verify 接口写回主数据。",
    }


@router.post("/v1/catalog/mapping/verify", summary="人工确认映射并写回主数据")
def mapping_verify(
    payload: MappingVerify,
    effective_tenant: str | None = Depends(tenant_guard("catalog.mapping.verify", "map_platform_sku")),
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_db),
) -> dict:
    if not effective_tenant:
        raise HTTPException(status_code=400, detail="人工确认必须指定租户")

    # 校验目标 SKU 属于当前租户，避免把 A 品牌的编码指到 B 品牌的商品上
    target = session.execute(
        select(Sku).where(Sku.sku_id == payload.sku_id)
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail=f"SKU {payload.sku_id} 不存在")
    if target.tenant_id != effective_tenant:
        raise HTTPException(status_code=403, detail=f"SKU {payload.sku_id} 不属于当前租户")

    existing = session.execute(
        select(PlatformSkuMap).where(
            PlatformSkuMap.tenant_id == effective_tenant,
            PlatformSkuMap.platform == payload.platform,
            PlatformSkuMap.shop_id == payload.shop_id,
            PlatformSkuMap.platform_sku_code == payload.platform_sku_code,
        )
    ).scalar_one_or_none()

    if existing is None:
        existing = PlatformSkuMap(
            tenant_id=effective_tenant,
            sku_id=payload.sku_id,
            platform=payload.platform,
            shop_id=payload.shop_id,
            platform_item_id=f"MANUAL-{payload.platform_sku_code}",
            platform_sku_code=payload.platform_sku_code,
        )
        session.add(existing)

    existing.sku_id = payload.sku_id
    existing.match_type = "manual"
    existing.confidence = 1.0
    existing.verified = True
    session.add(existing)
    session.commit()

    return {
        "verified": True,
        "operator": principal.username,
        "tenant_id": effective_tenant,
        "platform": payload.platform,
        "platform_sku_code": payload.platform_sku_code,
        "sku_id": payload.sku_id,
        "note": "已以人工确认写回主数据，match_type=manual、confidence=1.0。",
    }
