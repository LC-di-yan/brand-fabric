"""指标字典（Metric Registry）。

为什么指标要建字典而不是散落在 SQL 里
------------------------------------
跨平台报表最常见的翻车方式是：运营写一版 SQL 算 GMV、财务写另一版，
两边都"对"，但数字不一样，最后品牌方质疑数据能力。

本项目的做法是把指标的六要素固化进字典：
    口径定义 / 来源表 / 事件日期列 / 计算表达式 / 过滤条件 / Owner
并且**允许同一指标存在多个口径版本**，每个版本有独立的生效区间。
报表输出时必须回传口径版本号，历史数据用历史口径复算，不允许静默改定义。

版本机制的真实用例
------------------
`GMV_PAID` 有两个版本：
    v1.0  含预售              生效至 2026-08-31
    v1.1  剔除预售            2026-09-01 起生效
两种都能查、都能对账，差异可量化 —— 这正是口径治理要解决的问题。

关于 dt_column
--------------
同一张明细表里，不同指标要按不同的"事件日"归集：
下单类指标按下单日，支付类指标按支付日，退款类按退款日。
把它们混成一个日期列，日对账永远对不平。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from bdp.models import MetricDef

# (metric_code, name, version, definition, source_table, dt_column,
#  agg_expr, filter_expr, unit, platforms, owner, effective_from, effective_to)
DEFINITIONS: list[tuple] = [
    # ---------- 交易域：GMV 三口径 ----------
    (
        "GMV_ORDER", "下单 GMV", "v1.0",
        "统计周期内创建且未取消的订单实付金额合计。含未付款订单，用于运营侧看成交意愿。",
        "dwd_order", "order_dt", "SUM(pay_amount)",
        "is_valid = TRUE AND is_cancelled = FALSE",
        "CNY", "all", "运营数据组", date(2026, 1, 1), None,
    ),
    (
        "GMV_PAID", "支付 GMV（含预售）", "v1.0",
        "统计周期内完成支付的订单实付金额合计，不区分预售。历史期间的结算基线。",
        "dwd_order", "paid_dt", "SUM(pay_amount)",
        "is_valid = TRUE AND paid_dt IS NOT NULL",
        "CNY", "all", "财务数据组", date(2026, 1, 1), date(2026, 8, 31),
    ),
    (
        "GMV_PAID", "支付 GMV（剔除预售）", "v1.1",
        "统计周期内完成支付的订单实付金额合计，剔除预售订单。2026-09-01 起生效，"
        "与财务结算口径对齐，避免预售尾款造成的跨期波动。",
        "dwd_order", "paid_dt", "SUM(pay_amount)",
        "is_valid = TRUE AND paid_dt IS NOT NULL AND is_presale = FALSE",
        "CNY", "all", "财务数据组", date(2026, 9, 1), None,
    ),
    (
        "REFUND_AMOUNT", "退款金额", "v1.0",
        "统计周期内退款成功的金额合计，按退款发生日归集。",
        "dwd_refund", "refund_dt", "SUM(refund_amount)",
        "is_valid = TRUE AND refund_status = 'success'",
        "CNY", "all", "财务数据组", date(2026, 1, 1), None,
    ),
    (
        "GMV_SETTLE", "结算 GMV", "v1.0",
        "支付 GMV 减去同期退款金额，用于与品牌方对账。",
        "derived", "", "GMV_PAID - REFUND_AMOUNT", "",
        "CNY", "all", "财务数据组", date(2026, 1, 1), None,
    ),
    (
        "ORDER_CNT", "下单订单数", "v1.0",
        "统计周期内创建的订单行数（未取消）。",
        "dwd_order", "order_dt", "COUNT(*)",
        "is_valid = TRUE AND is_cancelled = FALSE",
        "笔", "all", "运营数据组", date(2026, 1, 1), None,
    ),
    (
        "PAID_CNT", "支付订单数", "v1.0",
        "统计周期内完成支付的订单行数。",
        "dwd_order", "paid_dt", "COUNT(*)",
        "is_valid = TRUE AND paid_dt IS NOT NULL",
        "笔", "all", "财务数据组", date(2026, 1, 1), None,
    ),
    (
        "AOV_PAID", "支付客单价", "v1.0",
        "支付 GMV / 支付订单数，反映单笔成交金额水平。",
        "derived", "", "GMV_PAID / PAID_CNT", "",
        "CNY", "all", "运营数据组", date(2026, 1, 1), None,
    ),
    (
        "REFUND_RATE", "退款率", "v1.0",
        "退款金额 / 支付 GMV。跨平台对比时必须使用同一口径，否则不可比。",
        "derived", "", "REFUND_AMOUNT / GMV_PAID", "",
        "比例", "all", "运营数据组", date(2026, 1, 1), None,
    ),
    # ---------- 客服域 ----------
    (
        "CS_SESSION_CNT", "客服会话量", "v1.0",
        "统计周期内产生的客服会话总数，去重后按会话发起日归集。",
        "dwd_cs_session", "session_dt", "COUNT(*)", "1 = 1",
        "次", "all", "客服运营组", date(2026, 1, 1), None,
    ),
    (
        "CS_BOT_CNT", "机器人接待量", "v1.0",
        "由智能客服（机器人）全程接待的会话数。",
        "dwd_cs_session", "session_dt", "SUM(CASE WHEN is_bot = TRUE THEN 1 ELSE 0 END)", "1 = 1",
        "次", "all", "客服运营组", date(2026, 1, 1), None,
    ),
    (
        "CS_BOT_RATIO", "机器人接待占比", "v1.0",
        "机器人接待量 / 客服会话总量。用于衡量智能客服的承接能力。",
        "derived", "", "CS_BOT_CNT / CS_SESSION_CNT", "",
        "比例", "all", "客服运营组", date(2026, 1, 1), None,
    ),
    (
        "CS_FIRST_RESP_AVG", "平均首响时长", "v1.0",
        "客服首次响应耗时的平均值（秒），机器人与人工混合统计。",
        "dwd_cs_session", "session_dt", "AVG(first_response_sec)", "1 = 1",
        "秒", "all", "客服运营组", date(2026, 1, 1), None,
    ),
    (
        "CS_SATISFACTION_AVG", "平均满意度", "v1.0",
        "有效满意度评分的平均值（1~5 分）。",
        "dwd_cs_session", "session_dt", "AVG(satisfaction)", "satisfaction IS NOT NULL",
        "分", "all", "客服运营组", date(2026, 1, 1), None,
    ),
    (
        "CS_RESOLVE_RATE", "一次解决率", "v1.0",
        "会话被标记为已解决的占比。",
        "dwd_cs_session", "session_dt", "AVG(CASE WHEN resolved = TRUE THEN 1.0 ELSE 0.0 END)", "1 = 1",
        "比例", "all", "客服运营组", date(2026, 1, 1), None,
    ),
]


def ensure_definitions(session: Session) -> int:
    """写入指标字典（幂等）。"""
    existing = {
        (code, ver)
        for code, ver in session.execute(select(MetricDef.metric_code, MetricDef.caliber_version)).all()
    }
    added = 0
    for row in DEFINITIONS:
        (code, name, ver, definition, src, dt_col, agg, filt, unit, platforms, owner, eff_from, eff_to) = row
        if (code, ver) in existing:
            continue
        session.add(
            MetricDef(
                metric_code=code, metric_name=name, caliber_version=ver, definition=definition,
                source_table=src, dt_column=dt_col, agg_expr=agg, filter_expr=filt, unit=unit,
                platforms=platforms, owner=owner, effective_from=eff_from, effective_to=eff_to, status=1,
            )
        )
        added += 1
    session.flush()
    return added


def _to_dict(r: MetricDef, include_detail: bool = True) -> dict:
    d = {
        "metric_code": r.metric_code,
        "metric_name": r.metric_name,
        "caliber_version": r.caliber_version,
        "unit": r.unit,
        "platforms": r.platforms,
        "owner": r.owner,
        "effective_from": r.effective_from.isoformat(),
        "effective_to": r.effective_to.isoformat() if r.effective_to else None,
        "is_derived": r.source_table == "derived",
    }
    if include_detail:
        d["definition"] = r.definition
    return d


def list_metrics(session: Session) -> list[dict]:
    rows = (
        session.execute(
            select(MetricDef).order_by(MetricDef.metric_code, MetricDef.caliber_version)
        )
        .scalars()
        .all()
    )
    return [_to_dict(r) for r in rows]


def resolve_version(session: Session, metric_code: str, on_date: date | None = None) -> MetricDef | None:
    """解析指标在某个日期应当使用的口径版本。

    规则：生效区间内、生效日期最晚的那一版。这就是"历史报表用历史口径"的实现。
    """
    on_date = on_date or date.today()
    candidates = (
        session.execute(
            select(MetricDef).where(MetricDef.metric_code == metric_code, MetricDef.status == 1)
        )
        .scalars()
        .all()
    )
    valid = [
        c for c in candidates
        if c.effective_from <= on_date and (c.effective_to is None or c.effective_to >= on_date)
    ]
    if not valid:
        return None
    return sorted(valid, key=lambda c: (c.effective_from, c.caliber_version))[-1]


def get_definition(session: Session, metric_code: str, caliber_version: str) -> MetricDef | None:
    return session.execute(
        select(MetricDef).where(
            MetricDef.metric_code == metric_code,
            MetricDef.caliber_version == caliber_version,
            MetricDef.status == 1,
        )
    ).scalar_one_or_none()


def list_versions(session: Session, metric_code: str) -> list[dict]:
    rows = (
        session.execute(
            select(MetricDef)
            .where(MetricDef.metric_code == metric_code)
            .order_by(MetricDef.effective_from)
        )
        .scalars()
        .all()
    )
    return [
        {
            "caliber_version": r.caliber_version,
            "metric_name": r.metric_name,
            "definition": r.definition,
            "effective_from": r.effective_from.isoformat(),
            "effective_to": r.effective_to.isoformat() if r.effective_to else None,
        }
        for r in rows
    ]
