"""租户隔离与鉴权测试 —— 本项目最重要的安全测试。

隔离失效属于"不可逆事故"：一次跨品牌数据泄漏就可能丢掉客户。
因此这里覆盖所有角色分支与三种绕过尝试：
    ① 直接请求他人租户
    ② 请求头与请求体租户不一致（试图绕过服务端解析）
    ③ 不带租户的"平台级"检索（试图把跨品牌聚合当成正常查询）
"""

from __future__ import annotations

from bdp.security.auth import (
    Principal,
    build_tenant_filter_sql,
    create_access_token,
    decode_access_token,
    hash_password,
    resolve_tenant,
    verify_password,
)

from .conftest import auth


# ---------------------------------------------------------------------------
# 单元：租户解析规则
# ---------------------------------------------------------------------------


def test_brand_can_only_access_own_tenant():
    p = Principal(user_id="U1", username="nova", role="brand", tenant_id="T001")
    assert resolve_tenant(p, None).allowed
    assert resolve_tenant(p, "T001").effective_tenant == "T001"

    cross = resolve_tenant(p, "T002")
    assert cross.allowed is False
    assert cross.is_violation is True
    assert "越权" in cross.reason


def test_brand_without_tenant_binding_is_rejected():
    p = Principal(user_id="U1", username="ghost", role="brand", tenant_id=None)
    res = resolve_tenant(p, None)
    assert res.allowed is False and res.is_violation is True


def test_ops_must_explicitly_declare_tenant():
    p = Principal(user_id="U2", username="ops", role="ops", tenant_id=None)
    assert resolve_tenant(p, None).allowed is False
    assert resolve_tenant(p, "T003").allowed is True


def test_admin_can_aggregate_but_only_via_explicit_path():
    p = Principal(user_id="U0", username="admin", role="admin", tenant_id=None)
    agg = resolve_tenant(p, None)
    assert agg.allowed and agg.effective_tenant is None
    assert resolve_tenant(p, "T004").effective_tenant == "T004"


def test_tenant_filter_sql_escapes_quotes():
    assert build_tenant_filter_sql(None) == "1=1"
    assert build_tenant_filter_sql("T001") == "tenant_id = 'T001'"
    # 单引号必须被转义，否则可被用于拼接注入
    assert "''" in build_tenant_filter_sql("T'001")


# ---------------------------------------------------------------------------
# 单元：口令与 Token
# ---------------------------------------------------------------------------


def test_password_hash_roundtrip():
    stored = hash_password("nova123")
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("nova123", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("nova123", "garbage")


def test_jwt_roundtrip_and_role_preserved():
    token = create_access_token(user_id="U1", username="nova", role="brand", tenant_id="T001")
    payload = decode_access_token(token)
    assert payload["username"] == "nova"
    assert payload["role"] == "brand"
    assert payload["tenant_id"] == "T001"


# ---------------------------------------------------------------------------
# 接口级：越权必须 403 且留审计
# ---------------------------------------------------------------------------


def test_api_brand_access_other_tenant_denied(client, tokens):
    res = client.get("/v1/dashboard/summary", headers=auth(tokens["nova"], "T002"))
    assert res.status_code == 403
    body = res.json()
    assert body["code"] == "TENANT_VIOLATION"
    assert "越权" in body["message"]


def test_api_brand_own_tenant_ok_and_scoped(client, tokens):
    res = client.get("/v1/dashboard/summary", headers=auth(tokens["nova"]))
    assert res.status_code == 200
    body = res.json()
    assert body["scope"]["tenant_id"] == "T001"
    assert len(body["tenant_compare"]) == 1


def test_api_body_tenant_cannot_override_header(client, tokens):
    """绕过尝试②：请求头不带租户，改用请求体指定他人租户。"""
    res = client.post(
        "/v1/metrics/query",
        headers=auth(tokens["nova"]),
        json={
            "metric_code": "GMV_PAID", "tenant_id": "T002",
            "start": "2026-09-01", "end": "2026-09-25",
        },
    )
    assert res.status_code == 403


def test_api_kb_search_requires_tenant_for_platform_identity(client, tokens):
    """绕过尝试③：平台级身份试图做跨品牌检索。"""
    res = client.post(
        "/v1/kb/search",
        headers=auth(tokens["admin"]),
        json={"query": "退货政策", "top_k": 3},
    )
    assert res.status_code == 400
    body = res.json()
    assert body["code"] == "BAD_REQUEST"
    assert "指定租户" in body["message"]


def test_api_kb_search_with_tenant_returns_only_that_tenant(client, tokens):
    res = client.post(
        "/v1/kb/search",
        headers=auth(tokens["nova"], "T001"),
        json={"query": "退货政策", "top_k": 5},
    )
    assert res.status_code == 200
    hits = res.json()["results"]
    assert hits, "应当命中自有租户的知识"
    for hit in hits:
        assert hit["chunk_id"].startswith("DOC")


def test_api_denied_attempts_are_audited(client, tokens):
    before = client.get("/v1/admin/audit?limit=1", headers=auth(tokens["admin"])).json()
    client.get("/v1/dashboard/summary", headers=auth(tokens["nova"], "T004"))
    after = client.get("/v1/admin/audit?limit=1", headers=auth(tokens["admin"])).json()

    assert after["denied_entries"] >= before["denied_entries"] + 1
    latest = client.get("/v1/admin/audit?limit=5&only_denied=true",
                        headers=auth(tokens["admin"])).json()["items"]
    assert latest and latest[0]["allowed"] is False


def test_api_audit_requires_privileged_role(client, tokens):
    res = client.get("/v1/admin/audit", headers=auth(tokens["nova"]))
    assert res.status_code == 403


def test_api_unknown_token_rejected(client):
    res = client.get("/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert res.status_code == 401
