"""数据模型定义（共 26 张表）。

分层约定
--------
dim_*    维度表：租户、店铺、商品（主数据域）
raw_*    贴源层：模拟"从平台 API / 业务库接入"的原始数据，字段未清洗
dwd_*    明细层：清洗、标准化、质量标记后的明细
dws_*    汇总层：按主题轻度汇总
metric_* 指标域：指标字典（含口径版本）与指标结果
dq_*     治理域：数据质量规则与执行结果
kb_*     知识域：知识文档与切片元数据
audit_log 安全域：租户越权审计

金额字段统一使用 Float：本项目为演示用途，避免 SQLite 下 Decimal 聚合的方言差异。
生产环境应改为 Numeric(18, 2)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """返回与时区无关的 UTC 时间，兼容 SQLite 与 PostgreSQL。"""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# 主数据域
# ---------------------------------------------------------------------------


class Tenant(Base):
    """品牌即租户。tenant_id 是贯穿全链路的强制隔离字段。"""

    __tablename__ = "dim_tenant"

    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    category_l1: Mapped[str] = mapped_column(String(32), nullable=False)
    category_l2: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Shop(Base):
    """店铺：同一品牌在多个平台有多个店铺。"""

    __tablename__ = "dim_shop"

    shop_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    shop_name: Mapped[str] = mapped_column(String(64), nullable=False)
    shop_type: Mapped[str] = mapped_column(String(16), default="flagship", nullable=False)
    status: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Spu(Base):
    """标准产品单元（集团统一口径）。"""

    __tablename__ = "dim_spu"

    spu_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    spu_name: Mapped[str] = mapped_column(String(128), nullable=False)
    category_l1: Mapped[str] = mapped_column(String(32), nullable=False)
    category_l2: Mapped[str] = mapped_column(String(32), nullable=False)
    brand_line: Mapped[str] = mapped_column(String(32), nullable=False)


class Sku(Base):
    """标准库存单元。sku_id 是集团内部统一编码。"""

    __tablename__ = "dim_sku"

    sku_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    spu_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    barcode: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    spec: Mapped[str] = mapped_column(String(64), nullable=False)
    list_price: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class PlatformSkuMap(Base):
    """跨平台商品映射：同一 SKU 在各平台有不同编码，这张表是主数据的核心产物。"""

    __tablename__ = "map_platform_sku"
    __table_args__ = (
        UniqueConstraint("tenant_id", "platform", "shop_id", "platform_sku_code", name="uq_platform_sku"),
        Index("ix_map_tenant_sku", "tenant_id", "sku_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    sku_id: Mapped[str] = mapped_column(String(32), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    shop_id: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    platform_sku_code: Mapped[str] = mapped_column(String(64), nullable=False)
    match_type: Mapped[str] = mapped_column(String(8), default="rule", nullable=False)  # rule|fuzzy|manual
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# 贴源层：模拟"从平台/业务系统接入"的原始数据（含脏数据）
# ---------------------------------------------------------------------------


class RawOrder(Base):
    __tablename__ = "raw_order"

    order_line_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    shop_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    # DQ004（退款不得超过订单金额）按 order_id 关联子查询逐行核对，
    # 此列无索引时该规则是 O(退款行 × 全表扫描)，属于质量校验里最贵的一条
    order_id: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    platform_sku_code: Mapped[str] = mapped_column(String(64), nullable=False)  # 原始编码，可能含全角/空格/大小写差异
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    pay_amount: Mapped[float | None] = mapped_column(Float, nullable=True)  # 可能为空或负数（脏数据）
    discount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    freight: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    order_status: Mapped[str] = mapped_column(String(16), nullable=False)
    is_presale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ingest_batch: Mapped[str] = mapped_column(String(32), nullable=False)


class RawRefund(Base):
    __tablename__ = "raw_refund"

    refund_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    shop_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    order_id: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    platform_sku_code: Mapped[str] = mapped_column(String(64), nullable=False)
    refund_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    refund_type: Mapped[str] = mapped_column(String(16), nullable=False)  # refund|return
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    refund_status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ingest_batch: Mapped[str] = mapped_column(String(32), nullable=False)


class RawCsSession(Base):
    __tablename__ = "raw_cs_session"

    session_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    shop_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(32), nullable=False)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_response_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    satisfaction: Mapped[float | None] = mapped_column(Float, nullable=True)
    intent_l1: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ingest_batch: Mapped[str] = mapped_column(String(32), nullable=False)


# ---------------------------------------------------------------------------
# 明细层（dwd）
# ---------------------------------------------------------------------------


class DwdOrder(Base):
    __tablename__ = "dwd_order"
    __table_args__ = (Index("ix_dwd_order_dt", "order_dt", "tenant_id"),)

    order_line_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    shop_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    order_id: Mapped[str] = mapped_column(String(40), nullable=False)
    sku_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    map_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    pay_amount: Mapped[float] = mapped_column(Float, nullable=False)
    discount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    freight: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    net_amount: Mapped[float] = mapped_column(Float, nullable=False)
    order_status: Mapped[str] = mapped_column(String(16), nullable=False)
    is_presale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_cancelled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    order_dt: Mapped[date] = mapped_column(Date, nullable=False)
    paid_dt: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    invalid_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DwdRefund(Base):
    __tablename__ = "dwd_refund"
    __table_args__ = (Index("ix_dwd_refund_dt", "refund_dt", "tenant_id"),)

    refund_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    shop_id: Mapped[str] = mapped_column(String(32), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    order_id: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    sku_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    refund_amount: Mapped[float] = mapped_column(Float, nullable=False)
    refund_type: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    refund_status: Mapped[str] = mapped_column(String(16), nullable=False)
    refund_dt: Mapped[date] = mapped_column(Date, nullable=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    invalid_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DwdCsSession(Base):
    __tablename__ = "dwd_cs_session"
    __table_args__ = (Index("ix_dwd_cs_dt", "session_dt", "tenant_id"),)

    session_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    shop_id: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(32), nullable=False)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_response_sec: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    satisfaction: Mapped[float | None] = mapped_column(Float, nullable=True)
    intent_l1: Mapped[str] = mapped_column(String(32), nullable=False)
    session_dt: Mapped[date] = mapped_column(Date, nullable=False)


# ---------------------------------------------------------------------------
# 汇总层（dws）
# ---------------------------------------------------------------------------


class DwsShopDay(Base):
    __tablename__ = "dws_shop_day"
    __table_args__ = (
        UniqueConstraint("dt", "shop_id", name="uq_dws_shop_day"),
        Index("ix_dws_shop_day_tenant", "dt", "tenant_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dt: Mapped[date] = mapped_column(Date, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    shop_id: Mapped[str] = mapped_column(String(32), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    # 订单（下单口径）
    order_cnt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    order_gmv: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # 支付口径
    paid_cnt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    paid_gmv: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # 退款与结算
    refund_cnt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    refund_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    settlement_gmv: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # 客服
    cs_session_cnt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cs_bot_cnt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cs_first_response_sum: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cs_resolved_cnt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cs_satisfaction_sum: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cs_satisfaction_cnt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class DwsTenantDay(Base):
    __tablename__ = "dws_tenant_day"
    __table_args__ = (
        UniqueConstraint("dt", "tenant_id", name="uq_dws_tenant_day"),
        Index("ix_dws_tenant_day_dt", "dt", "tenant_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dt: Mapped[date] = mapped_column(Date, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    order_cnt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    order_gmv: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    paid_gmv: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    refund_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    settlement_gmv: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cs_session_cnt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# ---------------------------------------------------------------------------
# 指标域
# ---------------------------------------------------------------------------


class MetricDef(Base):
    """指标字典。同一 metric_code 可以有多条不同 caliber_version 的记录 —— 口径版本管理。"""

    __tablename__ = "metric_def"
    __table_args__ = (UniqueConstraint("metric_code", "caliber_version", name="uq_metric_caliber"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    metric_code: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    metric_name: Mapped[str] = mapped_column(String(64), nullable=False)
    caliber_version: Mapped[str] = mapped_column(String(16), nullable=False)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    source_table: Mapped[str] = mapped_column(String(48), nullable=False)
    dt_column: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    agg_expr: Mapped[str] = mapped_column(String(160), nullable=False)
    filter_expr: Mapped[str] = mapped_column(String(240), default="1=1", nullable=False)
    unit: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    platforms: Mapped[str] = mapped_column(String(64), default="all", nullable=False)
    owner: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class MetricResult(Base):
    __tablename__ = "metric_result"
    __table_args__ = (
        UniqueConstraint(
            "metric_code", "caliber_version", "tenant_id", "dt", "dim_type", "dim_value",
            name="uq_metric_result",
        ),
        Index("ix_metric_result_lookup", "metric_code", "caliber_version", "dt"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    metric_code: Mapped[str] = mapped_column(String(48), nullable=False)
    caliber_version: Mapped[str] = mapped_column(String(16), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    dt: Mapped[date] = mapped_column(Date, nullable=False)
    dim_type: Mapped[str] = mapped_column(String(16), default="tenant", nullable=False)  # tenant|shop|platform
    dim_value: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# 治理域
# ---------------------------------------------------------------------------


class DqRule(Base):
    __tablename__ = "dq_rule"

    rule_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    table_name: Mapped[str] = mapped_column(String(48), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(16), nullable=False)  # not_null|range|unique|referential
    column_name: Mapped[str] = mapped_column(String(48), default="", nullable=False)
    expression: Mapped[str] = mapped_column(String(240), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), default="error", nullable=False)
    description: Mapped[str] = mapped_column(String(160), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class DqResult(Base):
    __tablename__ = "dq_result"
    __table_args__ = (Index("ix_dq_result_run", "run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(48), nullable=False)
    table_name: Mapped[str] = mapped_column(String(48), nullable=False)
    checked_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pass_rate: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    status: Mapped[str] = mapped_column(String(8), default="pass", nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# 知识域
# ---------------------------------------------------------------------------


class KbDocument(Base):
    __tablename__ = "kb_document"

    doc_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    kb_type: Mapped[str] = mapped_column(String(16), index=True, nullable=False)  # cs_faq|product|policy|sop
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class KbChunk(Base):
    __tablename__ = "kb_chunk"
    __table_args__ = (Index("ix_kb_chunk_doc", "doc_id", "chunk_ix"),)

    chunk_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    kb_type: Mapped[str] = mapped_column(String(16), nullable=False)
    chunk_ix: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    char_len: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(40), nullable=False)


# ---------------------------------------------------------------------------
# 安全域
# ---------------------------------------------------------------------------


class AppUser(Base):
    __tablename__ = "app_user"

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # admin|ops|brand
    tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    display_name: Mapped[str] = mapped_column(String(48), default="", nullable=False)
    # 凭证版本：修改密码后 +1，JWT 中的版本与用户当前版本不一致即判定凭证失效。
    # 这是"改密后旧 token 立即失效"的实现，比"靠过期时间等待"更符合企业级安全要求。
    token_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class AuditLog(Base):
    """租户越权审计：每一次跨租户访问尝试都必须留痕。"""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    user_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    username: Mapped[str] = mapped_column(String(48), default="", nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    resource: Mapped[str] = mapped_column(String(96), default="", nullable=False)
    requested_tenant: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    effective_tenant: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    detail: Mapped[str] = mapped_column(String(240), default="", nullable=False)


# ---------------------------------------------------------------------------
# Agent 协作域
# ---------------------------------------------------------------------------
# 这些表是多 agent 系统的"消息总线与共享记忆"，是纯新增的基础设施，
# 不与业务表发生外键关联（agent 层通过引用键如 ingest_batch_id 关联业务数据）。


class AgentRun(Base):
    """一次 DAG 运行（nightly 全链路 / 单 agent 按需任务 / goal 编排）。"""

    __tablename__ = "agent_run"

    run_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    dag_id: Mapped[str] = mapped_column(String(48), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), default="cli", nullable=False)  # cli|api|system|goal
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    # running | succeeded | partial_success | failed
    params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    stats: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # goal 编排溯源（P1：planner 产物；普通 DAG 运行为空）
    goal: Mapped[str] = mapped_column(Text, default="", nullable=False)
    plan: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class AgentTask(Base):
    """任务即消息：输入/输出是类型化 JSON，数据本体通过引用键（artifact）传递。"""

    __tablename__ = "agent_task"
    __table_args__ = (
        Index("ix_agent_task_run", "run_id", "name"),
        Index("ix_agent_task_status", "status"),
    )

    task_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)  # DAG 内唯一任务名
    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    # 依赖的是同 run 内的上游任务名列表（fan-out 子任务额外记录 parent）
    deps: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    parent_task: Mapped[str | None] = mapped_column(String(48), nullable=True)
    group: Mapped[str | None] = mapped_column(String(64), nullable=True)  # fan-out 分组
    params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    input: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    tenant_scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    # pending|running|waiting_approval|succeeded|degraded|failed|skipped|cancelled
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    timeout_sec: Mapped[int] = mapped_column(Integer, default=900, nullable=False)
    write_domains: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    lock_keys: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str] = mapped_column(String(480), default="", nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # ---- worker 租约（P0：执行体与 API 进程分离；inline 模式两列为空）----
    worker_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # ---- 审批闸口（P2：gate 动作 hold 时任务停在此状态等人工决定）----
    approved_by: Mapped[str] = mapped_column(String(32), default="", nullable=False)


class AgentArtifact(Base):
    """运行级黑板：上游任务的产出引用，下游凭 key 读取，不拷贝数据本体。"""

    __tablename__ = "agent_artifact"
    __table_args__ = (UniqueConstraint("run_id", "key", name="uq_agent_artifact"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    producer_task: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class AgentEvent(Base):
    """进度事件流：任务心跳、阶段完成、告警，供 API 与看板观察运行。"""
    __tablename__ = "agent_event"
    __table_args__ = (Index("ix_agent_event_run", "run_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(48), nullable=False)
    task_id: Mapped[str] = mapped_column(String(48), default="", nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    level: Mapped[str] = mapped_column(String(8), default="info", nullable=False)  # info|warn|error
    message: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class AgentMemory(Base):
    """跨 run 记忆（P3 episodic 层）：run 摘要 / agent 经验，供后续运行冷启动参考。

    scope: run_summary（run 结束时的聚合）| agent_note（agent 显式沉淀的经验）
    写入受写域守卫约束：默认 agent 无 memory 写权，spec 显式声明才有。
    """

    __tablename__ = "agent_memory"
    __table_args__ = (
        UniqueConstraint("scope", "scope_id", "key", name="uq_agent_memory"),
        Index("ix_agent_memory_scope", "scope", "scope_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)  # run_id / agent:tenant
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    produced_by: Mapped[str] = mapped_column(String(48), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # None = 永久


class InsightLog(Base):
    """InsightAgent 问答留痕：问题、答案、引用与工具调用轨迹可溯源。

    thread_id 支撑多轮会话（rag/session.py 按其读最近 N 轮做指代消解）；
    strategy/confidence 记录 RAG 管线模式与检索充分性（Agentic RAG）。
    """

    __tablename__ = "insight_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    thread_id: Mapped[str] = mapped_column(String(48), index=True, default="", nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, default="", nullable=False)
    citations: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    tool_trace: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    strategy: Mapped[str] = mapped_column(String(16), default="single", nullable=False)
    confidence: Mapped[str] = mapped_column(String(8), default="", nullable=False)
