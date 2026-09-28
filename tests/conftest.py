"""测试夹具。

关键点：**必须在导入 bdp 之前设置环境变量**，因为 bdp.config 里的 Settings
是模块级单例，导入后再改环境变量不会生效。这也是很多项目测试里
"数据被写进了开发库"的根因。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="bdp-test-"))

os.environ["BDP_MODE"] = "lite"
os.environ["BDP_DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["BDP_EMBEDDING_BACKEND"] = "hash"
os.environ["BDP_VECTOR_BACKEND"] = "local"
# 关键：把向量库指向临时目录。否则测试会与开发库互相污染
# （开发库里已有数据会让"应当为空"的断言失败，测试写入也会污染演示数据）。
os.environ["BDP_VECTOR_PATH"] = str(_TMP / "vector")
os.environ["BDP_MOCK_SEED"] = "20260926"
os.environ["BDP_JWT_SECRET"] = "test-secret-for-unit-tests-only-32bytes"

import pytest  # noqa: E402

TEST_DAYS = 5


def inject_dq_violations() -> None:
    """注入"必然违规"的记录：金额为空、金额为负、退款超额。

    随机注入概率太低，小样本下可能一次都没触发，导致质量用例变成看运气。
    显式注入才能让 DQ001/DQ002/DQ004 的断言稳定成立。
    可重复调用：测试改动共享数据后（如 agent 等价性测试重灌数据）可再次调用恢复。
    """
    from datetime import datetime

    from bdp.db import session_scope
    from bdp.models import RawOrder, RawRefund

    now = datetime(2026, 9, 23, 12, 0, 0)
    with session_scope() as session:
        base_order = session.query(RawOrder).limit(1).one()
        for suffix, amount in [("NULL", None), ("NEG", -88.0)]:
            session.add(RawOrder(
                order_line_id=f"DQ-INJ-{suffix}", tenant_id=base_order.tenant_id,
                shop_id=base_order.shop_id, platform=base_order.platform,
                order_id=f"DQ-INJ-ORD-{suffix}", platform_sku_code=base_order.platform_sku_code,
                qty=1, pay_amount=amount, discount=0.0, freight=0.0,
                order_status="paid", is_presale=False, created_at=now, paid_at=now,
                ingest_batch="DQ-INJ",
            ))
        session.add(RawOrder(
            order_line_id="DQ-INJ-GOOD", tenant_id=base_order.tenant_id,
            shop_id=base_order.shop_id, platform=base_order.platform,
            order_id="DQ-INJ-ORD-GOOD", platform_sku_code=base_order.platform_sku_code,
            qty=1, pay_amount=100.0, discount=0.0, freight=0.0,
            order_status="paid", is_presale=False, created_at=now, paid_at=now,
            ingest_batch="DQ-INJ",
        ))
        session.add(RawRefund(
            refund_id="DQ-INJ-R1", tenant_id=base_order.tenant_id,
            shop_id=base_order.shop_id, platform=base_order.platform,
            order_id="DQ-INJ-ORD-GOOD", platform_sku_code=base_order.platform_sku_code,
            refund_amount=50_000.0, refund_type="refund", reason_code="quality_issue",
            refund_status="success", created_at=now, ingest_batch="DQ-INJ",
        ))
        session.flush()


@pytest.fixture(scope="session")
def vector_dir() -> Path:
    return _TMP / "vector"


@pytest.fixture(scope="session")
def seeded():
    """构建一份小规模但完整的数据集：模拟数据 → 数仓 → 质量校验 → 指标。"""
    from datetime import date

    from bdp.db import init_db, session_scope
    from bdp.mock.generator import generate_all
    from bdp.metrics.engine import materialize
    from bdp.metrics.registry import ensure_definitions
    from bdp.pipeline.dwd import build_dwd
    from bdp.pipeline.dws import build_dws
    from bdp.pipeline.quality import ensure_rules, run_quality_checks

    init_db(drop=True)
    with session_scope() as session:
        info = generate_all(session, seed=20260926, days=TEST_DAYS)

    inject_dq_violations()

    with session_scope() as session:
        build_dwd(session)
    with session_scope() as session:
        build_dws(session)
        ensure_rules(session)
        run_quality_checks(session)
    with session_scope() as session:
        ensure_definitions(session)

    # 知识库也要入库，否则 KB 相关用例没有可检索数据
    from bdp.kb.ingest import ingest_knowledge
    from bdp.kb.store import build_store

    with session_scope() as session:
        ingest_knowledge(session, store=build_store(), rebuild=True)

    from sqlalchemy import text

    with session_scope() as session:
        start = session.execute(text("SELECT MIN(dt) FROM dws_tenant_day")).scalar()
        end = session.execute(text("SELECT MAX(dt) FROM dws_tenant_day")).scalar()
        materialize(session, start=date.fromisoformat(str(start)), end=date.fromisoformat(str(end)))

    info["start"] = date.fromisoformat(str(start))
    info["end"] = date.fromisoformat(str(end))
    return info


@pytest.fixture(scope="session")
def client(seeded):
    """带上已初始化数据的 FastAPI 测试客户端。"""
    from fastapi.testclient import TestClient

    from bdp.api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def tokens(client) -> dict[str, str]:
    out = {}
    for username, password in [
        ("admin", "admin123"), ("ops", "ops123"),
        ("nova", "nova123"), ("aurora", "aurora123"),
    ]:
        res = client.post("/v1/auth/token", json={"username": username, "password": password})
        assert res.status_code == 200, res.text
        out[username] = res.json()["access_token"]
    return out


def auth(token: str, tenant: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if tenant:
        headers["X-Tenant-Id"] = tenant
    return headers
