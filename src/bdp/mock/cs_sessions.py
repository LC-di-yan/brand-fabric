"""客服会话数据生成（机器人/人工差异 + 重复会话注入）。

从 generator 拆出。
"""

from __future__ import annotations

import random

from datetime import datetime, time

from sqlalchemy.orm import Session

from bdp.mock.brands import CS_INTENTS, SPEC_BY_ID, _iter_days
from bdp.models import RawCsSession

def _gen_cs_sessions(
    session: Session, rng: random.Random, shops: list[dict], days: int
) -> int:
    rows: list[dict] = []
    batch = f"B{datetime.now().strftime('%Y%m%d%H%M')}"
    seq = 0
    for d in _iter_days(days):
        for shop in shops:
            spec = SPEC_BY_ID[shop["tenant_id"]]
            n = max(2, int(rng.gauss(spec.cs_base_per_day, spec.cs_base_per_day * 0.25)))
            for i in range(n):
                seq += 1
                is_bot = rng.random() < spec.bot_ratio
                # 机器人首响快、满意度略低；人工则反之
                if is_bot:
                    fr = max(1.0, rng.gauss(2.5, 1.2))
                    satisfaction = rng.gauss(4.35, 0.5)
                else:
                    fr = max(3.0, rng.gauss(spec.first_resp_mean, spec.first_resp_mean * 0.5))
                    satisfaction = rng.gauss(4.62, 0.35)
                seq_id = f"CS{d.strftime('%y%m%d')}{seq:06d}"
                # 脏数据：0.5% 重复会话（同一 session_id 出现两次，主键冲突由去重逻辑处理）
                if rng.random() < 0.005:
                    seq_id = f"CS{d.strftime('%y%m%d')}{max(1, seq - 1):06d}"
                rows.append(
                    dict(
                        session_id=seq_id,
                        tenant_id=spec.tenant_id,
                        shop_id=shop["shop_id"],
                        agent_id="BOT" if is_bot else f"AG{rng.randint(1, 240):04d}",
                        is_bot=is_bot,
                        first_response_sec=round(fr, 1),
                        turn_count=rng.randint(1, 18),
                        resolved=rng.random() < (0.88 if is_bot else 0.93),
                        satisfaction=round(min(5.0, max(1.0, satisfaction)), 2),
                        intent_l1=rng.choices(
                            CS_INTENTS, weights=[0.22, 0.20, 0.16, 0.09, 0.08, 0.05, 0.06, 0.09, 0.03, 0.02]
                        )[0],
                        created_at=datetime.combine(
                            d, time(rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))
                        ),
                        ingest_batch=batch,
                    )
                )
    # 主键去重（保留第一条），用于演示"上游重复推送"
    dedup: dict[str, dict] = {}
    for r in rows:
        dedup.setdefault(r["session_id"], r)
    session.bulk_insert_mappings(RawCsSession, list(dedup.values()))
    session.flush()
    return len(dedup)
