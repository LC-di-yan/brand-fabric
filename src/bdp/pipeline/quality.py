"""数据质量校验。

设计取舍
--------
很多 demo 项目默认"上游数据是干净的"，被问"你怎么保证数据质量"时哑口无言。
本项目反过来：**模拟数据里故意注入了 5 类脏数据**，并在这里定义规则去捕获它们，
产出通过率报表。这样才能证明链路是有门禁的，而不是"刚好能跑"。

规则表达方式统一为「有效行谓词」：expression 描述的是**应该满足的条件**，
failed_rows = 总行数 - 满足条件的行数。这样做的好处是规则可读、可复用，
不需要为每种校验写一段专用代码。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from bdp.models import DqResult, DqRule

# rule_id, table, rule_type, column, valid_predicate, severity, description
DEFAULT_RULES: list[tuple[str, str, str, str, str, str, str]] = [
    (
        "DQ001", "raw_order", "not_null", "pay_amount",
        "pay_amount IS NOT NULL", "error",
        "订单实付金额不能为空（上游平台偶发回传空值）",
    ),
    (
        "DQ002", "raw_order", "range", "pay_amount",
        "pay_amount > 0", "error",
        "订单实付金额必须为正数（上游存在负数冲正记录）",
    ),
    (
        "DQ003", "raw_order", "range", "qty",
        "qty > 0 AND qty <= 100", "warn",
        "订单数量需在合理区间（1~100）",
    ),
    (
        "DQ004", "raw_refund", "business", "refund_amount",
        "refund_amount > 0 AND refund_amount <= "
        "(SELECT MAX(o.pay_amount) FROM raw_order o WHERE o.order_id = raw_refund.order_id)",
        "error",
        "退款金额不得超过对应订单的实付金额",
    ),
    (
        "DQ005", "dwd_order", "referential", "sku_id",
        "sku_id IS NOT NULL", "warn",
        "订单明细必须能映射到集团内部 SKU（主数据覆盖率）",
    ),
    (
        "DQ006", "dwd_order", "range", "net_amount",
        "net_amount >= 0", "error",
        "商品实收金额不得为负",
    ),
]


def ensure_rules(session: Session) -> int:
    """写入内置规则（幂等）。"""
    existing = {r for (r,) in session.execute(select(DqRule.rule_id)).all()}
    added = 0
    for rule_id, table, rtype, column, expr, severity, desc in DEFAULT_RULES:
        if rule_id in existing:
            continue
        session.add(
            DqRule(
                rule_id=rule_id, table_name=table, rule_type=rtype, column_name=column,
                expression=expr, severity=severity, description=desc, enabled=True,
            )
        )
        added += 1
    session.flush()
    return added


def _count(session: Session, sql: str) -> int:
    return int(session.execute(text(sql)).scalar() or 0)


def run_quality_checks(session: Session) -> list[dict]:
    """执行全部启用规则，写入 dq_result，返回结果列表。"""
    run_id = f"RUN{datetime.now().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:4]}"
    rules = session.execute(select(DqRule).where(DqRule.enabled.is_(True))).scalars().all()
    results: list[dict] = []

    for rule in rules:
        table = rule.table_name
        total = _count(session, f"SELECT COUNT(*) FROM {table}")
        failed = 0

        if rule.rule_type == "unique":
            distinct = _count(session, f"SELECT COUNT(DISTINCT {rule.column_name}) FROM {table}")
            failed = total - distinct
        else:
            failed = _count(session, f"SELECT COUNT(*) FROM {table} WHERE NOT ({rule.expression})")

        pass_rate = round((total - failed) / total, 6) if total else 1.0
        status = "pass"
        if failed > 0:
            status = "error" if rule.severity == "error" else "warn"

        session.add(
            DqResult(
                run_id=run_id, rule_id=rule.rule_id, table_name=table,
                checked_rows=total, failed_rows=failed, pass_rate=pass_rate,
                status=status, detail=rule.description,
            )
        )
        results.append(
            {
                "run_id": run_id,
                "rule_id": rule.rule_id,
                "table": table,
                "severity": rule.severity,
                "checked_rows": total,
                "failed_rows": failed,
                "pass_rate": pass_rate,
                "status": status,
                "description": rule.description,
            }
        )

    session.flush()
    return results


def quality_summary(session: Session) -> dict:
    """最近一次校验的汇总，供看板展示。"""
    latest_run = session.execute(select(func.max(DqResult.run_id))).scalar()
    if not latest_run:
        return {"run_id": None, "rules": 0, "failed_rules": 0, "overall_pass_rate": 1.0, "details": []}

    rows = (
        session.execute(select(DqResult).where(DqResult.run_id == latest_run))
        .scalars()
        .all()
    )
    total_checked = sum(r.checked_rows for r in rows)
    total_failed = sum(r.failed_rows for r in rows)
    return {
        "run_id": latest_run,
        "rules": len(rows),
        "failed_rules": sum(1 for r in rows if r.failed_rows > 0),
        "overall_pass_rate": round((total_checked - total_failed) / total_checked, 6) if total_checked else 1.0,
        "details": [
            {
                "rule_id": r.rule_id, "table": r.table_name, "severity": r.status,
                "checked_rows": r.checked_rows, "failed_rows": r.failed_rows,
                "pass_rate": r.pass_rate, "description": r.detail,
            }
            for r in rows
        ],
    }
