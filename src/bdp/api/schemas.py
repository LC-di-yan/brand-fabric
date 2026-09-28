"""请求 / 响应模型。"""

from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel, Field, field_validator

# 用户名白名单字符集（登录输入校验）
_USERNAME_RE = re.compile(r"[A-Za-z0-9_.\-]{1,48}")


class TokenRequest(BaseModel):
    """登录请求：严格白名单校验，异常输入在进入任何查询前即被拒绝。"""

    username: str = Field(min_length=1, max_length=48, examples=["nova"])
    password: str = Field(min_length=1, max_length=128, examples=["nova123"])
    captcha_id: str | None = Field(default=None, max_length=256)
    captcha_code: str | None = Field(default=None, max_length=8)

    @field_validator("username")
    @classmethod
    def _username_charset(cls, v: str) -> str:
        # 白名单字符集：杜绝控制字符、引号、空白等注入面（SQL 层本身已是参数化查询）
        if not _USERNAME_RE.fullmatch(v):
            raise ValueError("用户名仅允许字母、数字、下划线、点与连字符")
        return v


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=128, examples=["nova123"])
    new_password: str = Field(min_length=1, max_length=128, examples=["nova2026abc"])


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str
    tenant_id: str | None = None
    expires_in_minutes: int
    # CSRF 双提交令牌：浏览器模式由 Set-Cookie 下发；file:// 本地模式取此字段
    csrf_token: str | None = None


class PrincipalOut(BaseModel):
    user_id: str
    username: str
    role: str
    tenant_id: str | None
    is_platform_level: bool


class MetricQuery(BaseModel):
    metric_code: str = Field(examples=["GMV_PAID"])
    caliber_version: str | None = Field(default=None, examples=["v1.1"])
    tenant_id: str | None = Field(
        default=None,
        description="仅 ops / admin 可用；brand 账号传入将被判定为越权并记录审计",
    )
    dim_type: str = Field(default="tenant", examples=["tenant", "shop", "platform"])
    compare: str = Field(default="none", examples=["none", "mom", "yoy"], description="环比 mom / 同比 yoy")
    start: date
    end: date
    dim_value: str | None = Field(default=None, description="按维度值过滤（如指定 shop_id）")


class MetricPoint(BaseModel):
    dt: str
    tenant_id: str
    dim_value: str
    value: float


class MetricQueryResult(BaseModel):
    metric_code: str
    metric_name: str
    caliber_version: str
    unit: str
    definition: str
    owner: str
    dim_type: str
    start: str
    end: str
    total: float | None
    additive_total: float
    point_count: int
    points: list[MetricPoint]
    formula: str | None = None
    compare: dict | None = None
    caliber_notice: str = Field(
        description="口径提示，必须随报表一并展示，避免历史数据被新口径静默改写"
    )


class KbSearchRequest(BaseModel):
    query: str = Field(examples=["退货政策是怎样的"])
    tenant_id: str | None = None
    kb_type: str | None = Field(default=None, examples=["cs_faq", "product", "policy", "sop"])
    top_k: int = 5
    mode: str = Field(default="hybrid", examples=["dense", "sparse", "hybrid"])
    rerank: bool = True


class KbSearchHit(BaseModel):
    chunk_id: str
    doc_id: str
    kb_type: str
    chunk_ix: int
    text: str
    score: float
    rrf_score: float
    lexical_score: float
    source: str


class KbSearchResponse(BaseModel):
    query: str
    tenant_id: str
    kb_type: str
    mode: str
    rerank: bool
    candidates: int
    results: list[KbSearchHit]
    warning: str | None = None


class KbDocumentUpsert(BaseModel):
    kb_type: str = Field(examples=["cs_faq"])
    source_id: str = Field(default="MANUAL", examples=["MANUAL"])
    title: str = Field(examples=["退货政策补充说明"])
    content: str = Field(examples=["新增规则：大件商品退货需保留原包装。"])


class MappingVerify(BaseModel):
    platform: str = Field(examples=["tmall"])
    shop_id: str = Field(examples=["S10101"])
    platform_sku_code: str = Field(examples=["TMALL-0000123"])
    sku_id: str = Field(examples=["T001-SPU0001-SKU01"])
    note: str | None = None
