"""交易数据生成：订单与退款（含金额异常与编码污染注入）。

从 generator 拆出。注入比例刻意很小（千分之几），保证质量校验捕获的是
"真实分布下的少数派"，而不是让大部分数据都是脏的。
"""

from __future__ import annotations

import random

from datetime import datetime, time, timedelta

from sqlalchemy.orm import Session

from bdp.mock.brands import ORDER_STATUS, REFUND_REASONS, SPEC_BY_ID, _corrupt_code, _iter_days, _promo_multiplier
from bdp.models import RawOrder, RawRefund

def _gen_transactions(
    session: Session,
    rng: random.Random,
    skus: list[dict],
    shops: list[dict],
    days: int,
    code_lookup: dict[tuple[str, str, str, str], str],
) -> tuple[int, int]:
    skus_by_tenant: dict[str, list[dict]] = {}
    for s in skus:
        skus_by_tenant.setdefault(s["tenant_id"], []).append(s)

    order_rows: list[dict] = []
    refund_rows: list[dict] = []
    batch = f"B{datetime.now().strftime('%Y%m%d%H%M')}"
    order_seq = 0
    refund_seq = 0

    for d in _iter_days(days):
        for shop in shops:
            spec = SPEC_BY_ID[shop["tenant_id"]]
            tenant_skus = skus_by_tenant[shop["tenant_id"]]
            if not tenant_skus:
                continue

            base = {
                "T001": 96, "T002": 40, "T003": 52, "T004": 62,
            }[spec.tenant_id]
            # 店铺权重：品牌首个店铺体量更大
            shop_weight = 1.0 if shop["shop_id"].endswith("01") else 0.65
            multiplier = _promo_multiplier(d, spec.promo_affinity)
            n_orders = max(3, int(rng.gauss(base * shop_weight * multiplier, base * 0.18)))

            for _ in range(n_orders):
                order_seq += 1
                sku = rng.choice(tenant_skus)
                qty = rng.choices([1, 2, 3], weights=[0.82, 0.14, 0.04])[0]
                unit = round(sku["list_price"] * rng.uniform(0.62, 1.0), 2)
                pay_amount: float | None = round(unit * qty, 2)
                discount = round(unit * qty * rng.uniform(0.0, 0.28), 2)
                freight = 0.0 if pay_amount >= 199 else 12.0

                # 脏数据：0.3% 的订单金额异常
                dirty = rng.random()
                if dirty < 0.0018:
                    pay_amount = None
                elif dirty < 0.0030:
                    pay_amount = -abs(pay_amount or 100.0)

                status = rng.choices(
                    ORDER_STATUS, weights=[0.30, 0.28, 0.34, 0.06, 0.02]
                )[0]
                created = datetime.combine(d, time(rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59)))
                paid_at = created + timedelta(seconds=rng.randint(5, 1800)) if status != "cancelled" else None

                order_id = f"O{d.strftime('%y%m%d')}{order_seq:07d}"
                # 订单里带的是**平台侧编码**（平台不认识内部条码）
                plat_code = code_lookup.get(
                    (spec.tenant_id, shop["platform"], shop["shop_id"], sku["sku_id"]),
                    sku["barcode"],
                )
                # 2% 的订单编码与映射表登记值不一致，且归一化修不了 → 只能靠模糊匹配
                if rng.random() < 0.02:
                    plat_code = _corrupt_code(rng, plat_code)
                order_rows.append(
                    dict(
                        order_line_id=f"{order_id}-1",
                        tenant_id=spec.tenant_id,
                        shop_id=shop["shop_id"],
                        platform=shop["platform"],
                        order_id=order_id,
                        platform_sku_code=plat_code,
                        qty=qty,
                        pay_amount=pay_amount,
                        discount=discount,
                        freight=freight,
                        order_status=status,
                        is_presale=rng.random() < 0.06,
                        created_at=created,
                        paid_at=paid_at,
                        ingest_batch=batch,
                    )
                )

                # 退款：按品牌退款率生成
                if status != "cancelled" and pay_amount and rng.random() < spec.refund_rate:
                    refund_seq += 1
                    refund_amount = round(
                        abs(pay_amount) * rng.uniform(0.3, 1.0), 2
                    )
                    # 脏数据：0.2% 的退款金额超过订单金额
                    if rng.random() < 0.002:
                        refund_amount = round(abs(pay_amount) * rng.uniform(1.2, 2.5), 2)
                    refund_rows.append(
                        dict(
                            refund_id=f"R{d.strftime('%y%m%d')}{refund_seq:07d}",
                            tenant_id=spec.tenant_id,
                            shop_id=shop["shop_id"],
                            platform=shop["platform"],
                            order_id=order_id,
                            platform_sku_code=plat_code,
                            refund_amount=refund_amount,
                            refund_type=rng.choices(["refund", "return"], weights=[0.55, 0.45])[0],
                            reason_code=rng.choice(REFUND_REASONS),
                            refund_status=rng.choices(
                                ["success", "processing", "rejected"], weights=[0.86, 0.10, 0.04]
                            )[0],
                            created_at=created + timedelta(days=rng.randint(1, 12)),
                            ingest_batch=batch,
                        )
                    )

    session.bulk_insert_mappings(RawOrder, order_rows)
    session.bulk_insert_mappings(RawRefund, refund_rows)
    session.flush()
    return len(order_rows), len(refund_rows)
