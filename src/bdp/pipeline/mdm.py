"""跨平台商品主数据映射。

问题定义
--------
同一个集团内部 SKU，在天猫/京东/抖音/拼多多/小红书各店铺有**完全不同的平台侧编码**，
而且上游编码不规范（大小写、空格、全角字符，甚至偶尔缺位）。
订单明细里带的是平台侧编码，所有分析都必须先把它还原成内部 sku_id。

三级匹配策略
------------
1. **规则匹配**：归一化后精确相等 —— 覆盖绝大多数，置信度 1.0
2. **模糊匹配**：归一化后仍不等（如上游缺位），用编辑距离兜底，置信度 = 相似度
3. **人工复核**：置信度低于阈值的不强行映射，进复核队列

为什么不做"全表模糊匹配"
------------------------
模糊匹配是 O(n·m) 的，10 万订单 × 900 SKU 直接算会非常慢。
实践中先做精确哈希命中，只把未命中的少数记录送入模糊匹配 —— 这也是生产环境的常规做法。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from bdp.models import PlatformSkuMap
from bdp.pipeline.normalize import normalize_code

FUZZY_CUTOFF = 88.0  # 低于该相似度不自动映射


@dataclass
class MatchResult:
    sku_id: str | None
    confidence: float
    match_type: str  # exact | fuzzy | none


@dataclass
class MdmStats:
    total: int = 0
    exact: int = 0
    fuzzy: int = 0
    unmatched: int = 0
    conflicts: list[str] = field(default_factory=list)
    unmatched_samples: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return (self.exact + self.fuzzy) / self.total if self.total else 0.0

    @property
    def match_rate(self) -> float:
        return (self.exact + self.fuzzy) / self.total if self.total else 0.0


class PlatformSkuMatcher:
    """平台编码 → 内部 SKU 的匹配器。

    索引在构建时一次性建立，之后匹配是内存操作，适合批量加工场景。
    """

    def __init__(self) -> None:
        # (tenant_id, platform, shop_id, norm_code) -> sku_id
        self._exact: dict[tuple[str, str, str, str], str] = {}
        # (tenant_id, platform, norm_code) -> sku_id   跨店铺回退索引
        self._platform_exact: dict[tuple[str, str, str], str] = {}
        # tenant_id -> (codes[], sku_ids[])  仅用于未命中时的模糊匹配
        self._fuzzy_pool: dict[str, tuple[list[str], list[str]]] = {}
        self.conflicts: list[str] = []

    # -- 构建 ---------------------------------------------------------------

    @classmethod
    def build(cls, session: Session) -> "PlatformSkuMatcher":
        matcher = cls()

        rows = session.execute(
            select(
                PlatformSkuMap.tenant_id,
                PlatformSkuMap.platform,
                PlatformSkuMap.shop_id,
                PlatformSkuMap.platform_sku_code,
                PlatformSkuMap.sku_id,
            )
        ).all()

        pool_codes: dict[str, list[str]] = defaultdict(list)
        pool_skus: dict[str, list[str]] = defaultdict(list)

        for tenant_id, platform, shop_id, raw_code, sku_id in rows:
            code = normalize_code(raw_code)
            if not code:
                continue
            key = (tenant_id, platform, shop_id, code)
            existing = matcher._exact.get(key)
            if existing is not None and existing != sku_id:
                # 归一化后出现冲突：同一平台编码指向两个内部 SKU，属于主数据质量问题
                matcher.conflicts.append(f"{tenant_id}/{platform}/{shop_id}/{code}: {existing} vs {sku_id}")
            else:
                matcher._exact[key] = sku_id

            pkey = (tenant_id, platform, code)
            if pkey not in matcher._platform_exact:
                matcher._platform_exact[pkey] = sku_id

            pool_codes[tenant_id].append(code)
            pool_skus[tenant_id].append(sku_id)

        matcher._fuzzy_pool = {
            t: (codes, pool_skus[t]) for t, codes in pool_codes.items()
        }
        return matcher

    @property
    def exact_index_size(self) -> int:
        return len(self._exact)

    # -- 匹配 ---------------------------------------------------------------

    def match(self, tenant_id: str, platform: str, shop_id: str, raw_code: str | None) -> MatchResult:
        code = normalize_code(raw_code)
        if not code:
            return MatchResult(None, 0.0, "none")

        # 一级：店铺内精确
        hit = self._exact.get((tenant_id, platform, shop_id, code))
        if hit:
            return MatchResult(hit, 1.0, "exact")

        # 二级：同品牌同平台跨店铺精确（平台编码在同一平台内通常唯一）
        hit = self._platform_exact.get((tenant_id, platform, code))
        if hit:
            return MatchResult(hit, 0.99, "exact")

        # 三级：同品牌内模糊兜底（仅对未命中记录调用，避免全表模糊匹配）
        pool = self._fuzzy_pool.get(tenant_id)
        if not pool or not pool[0]:
            return MatchResult(None, 0.0, "none")

        codes, sku_ids = pool
        best = process.extractOne(code, codes, scorer=fuzz.ratio, score_cutoff=FUZZY_CUTOFF)
        if best is None:
            return MatchResult(None, 0.0, "none")

        _matched_code, score, idx = best
        return MatchResult(sku_ids[idx], round(float(score) / 100.0, 3), "fuzzy")

    def candidates(
        self, tenant_id: str, platform: str, raw_code: str | None, n: int = 3
    ) -> list[dict]:
        """给人工复核用的候选列表：返回相似度最高的前 n 个候选 SKU。

        复核界面需要的是"让用户选"，而不是"替用户决定"。
        """
        code = normalize_code(raw_code)
        if not code:
            return []
        pool = self._fuzzy_pool.get(tenant_id)
        if not pool or not pool[0]:
            return []
        codes, sku_ids = pool
        best = process.extract(code, codes, scorer=fuzz.ratio, limit=n)
        return [
            {"code": matched, "score": round(float(score) / 100.0, 3), "sku_id": sku_ids[idx]}
            for matched, score, idx in best
        ]


def resolve_orders(
    session: Session, matcher: PlatformSkuMatcher, raw_orders: list[dict]
) -> tuple[dict[str, MatchResult], MdmStats]:
    """批量解析订单里的平台编码。

    返回 {(tenant_id, platform, shop_id, order_line_id): MatchResult} 与统计。
    相同编码会被缓存，避免每次重复匹配。
    """
    cache: dict[tuple[str, str, str, str], MatchResult] = {}
    resolved: dict[str, MatchResult] = {}
    stats = MdmStats()
    stats.conflicts = matcher.conflicts

    for row in raw_orders:
        stats.total += 1
        cache_key = (row["tenant_id"], row["platform"], row["shop_id"], normalize_code(row["platform_sku_code"]))
        result = cache.get(cache_key)
        if result is None:
            result = matcher.match(
                row["tenant_id"], row["platform"], row["shop_id"], row["platform_sku_code"]
            )
            cache[cache_key] = result

        resolved[row["order_line_id"]] = result
        if result.match_type == "exact":
            stats.exact += 1
        elif result.match_type == "fuzzy":
            stats.fuzzy += 1
        else:
            stats.unmatched += 1
            if len(stats.unmatched_samples) < 20:
                stats.unmatched_samples.append(
                    f"{row['tenant_id']}/{row['platform']}/{row['platform_sku_code']}"
                )

    return resolved, stats
