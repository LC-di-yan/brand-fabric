"""登录安全与凭证管理测试。

覆盖登录限流（防暴力破解）与"改密后旧凭证立即失效"——
这两条都是企业级系统必备、且很多 demo 项目缺失的能力。
"""

from __future__ import annotations

import pytest

from bdp.security.ratelimit import LoginRateLimiter, RateLimited

from .conftest import auth


# ---------------------------------------------------------------------------
# 限流器单元
# ---------------------------------------------------------------------------


def test_rate_limiter_locks_after_max_failures():
    limiter = LoginRateLimiter(max_failures=3, lock_seconds=60)
    key = "user-a"
    assert limiter.record_failure(key) == 2
    assert limiter.record_failure(key) == 1
    assert limiter.record_failure(key) == 0
    with pytest.raises(RateLimited):
        limiter.check(key)
    # 锁定期间即使密码正确也拒绝（由调用方在 check 之后判定）
    assert limiter.lock_remaining_seconds(key) > 0


def test_rate_limiter_success_clears_failures():
    limiter = LoginRateLimiter(max_failures=3, lock_seconds=60)
    key = "user-b"
    limiter.record_failure(key)
    limiter.record_failure(key)
    limiter.record_success(key)
    assert limiter.remaining_attempts(key) == 3
    limiter.check(key)  # 不应抛出


# ---------------------------------------------------------------------------
# 接口级
# ---------------------------------------------------------------------------


def test_login_lockout_and_recovery_hint(client):
    """连续失败触发锁定，锁定期间即使密码正确也拒绝。

    使用不参与其他用例的账号，且先清空限流状态，避免用例间互相污染。
    安全要求：失败响应不得区分账号是否存在、不得返回剩余尝试次数。
    达到验证码阈值后的尝试需携带正确验证码，以验证"锁定优先于凭据"。
    """
    from bdp.security import captcha as cap_mod
    from bdp.security.ratelimit import login_rate_limiter

    username = "verde"
    login_rate_limiter.record_success(username)  # 清空历史计数，保证用例独立

    for i in range(5):
        body_req: dict = {"username": username, "password": "wrong-pass-000"}
        if i >= 3:  # 阈值之后必须带验证码，否则返回 400 而非 401
            cap = client.get("/v1/auth/captcha").json()
            answer = cap_mod._unsign(cap["captcha_id"]).split("|")[-2]
            body_req.update(captcha_id=cap["captcha_id"], captcha_code=answer)
        res = client.post("/v1/auth/token", json=body_req)
        assert res.status_code == 401
        body = res.json()
        assert body["code"] == "UNAUTHORIZED"
        assert body["message"] == "账号或密码错误"
        # 不泄露账号存在性与尝试进度
        assert "remaining_attempts" not in body

    locked = client.post("/v1/auth/token", json={"username": username, "password": "wrong-pass-000"})
    assert locked.status_code == 429
    body = locked.json()
    assert body["code"] == "RATE_LIMITED"
    assert body["retry_after_seconds"] > 0

    st = client.post("/v1/auth/token", json={"username": username, "password": "verde123"})
    assert st.status_code == 429

    # 清理，避免锁定状态影响其他用例
    login_rate_limiter.record_success(username)


def test_login_failure_responses_are_indistinguishable(client):
    """账号不存在与密码错误的 401 响应体必须字节级一致（防账号枚举）。"""
    from bdp.security.ratelimit import login_rate_limiter

    login_rate_limiter.record_success("ghost-user-xyz")
    login_rate_limiter.record_success("nova")

    unknown = client.post("/v1/auth/token", json={"username": "ghost-user-xyz", "password": "whatever1"})
    wrong = client.post("/v1/auth/token", json={"username": "nova", "password": "wrong-pass-1"})
    assert unknown.status_code == wrong.status_code == 401
    # 去掉服务端注入的 request_id 后，业务字段完全一致
    a, b = dict(unknown.json()), dict(wrong.json())
    for d in (a, b):
        d.pop("request_id", None)
        d.pop("ts", None)
    assert a == b
    login_rate_limiter.record_success("nova")


def test_login_captcha_required_after_threshold(client):
    """连续失败达到阈值后，未带验证码的登录被要求补充验证码。"""
    from bdp.security.ratelimit import login_rate_limiter

    username = "lumen"
    login_rate_limiter.record_success(username)

    for _ in range(3):
        client.post("/v1/auth/token", json={"username": username, "password": "bad-pass-0"})

    res = client.post("/v1/auth/token", json={"username": username, "password": "lumen123"})
    assert res.status_code == 400
    body = res.json()
    assert body["code"] == "CAPTCHA_REQUIRED"
    assert body.get("captcha_required") is True

    # 带错误验证码 → 仍被拒；带正确验证码 → 放行（答案可从签名 token 反解仅测试用）
    cap = client.get("/v1/auth/captcha").json()
    wrong_cap = client.post("/v1/auth/token", json={
        "username": username, "password": "lumen123",
        "captcha_id": cap["captcha_id"], "captcha_code": "99999",
    })
    assert wrong_cap.status_code == 400

    from bdp.security import captcha as cap_mod
    answer = cap_mod._unsign(cap["captcha_id"]).split("|")[-2]
    ok = client.post("/v1/auth/token", json={
        "username": username, "password": "lumen123",
        "captcha_id": cap["captcha_id"], "captcha_code": answer,
    })
    assert ok.status_code == 200
    login_rate_limiter.record_success(username)


def test_captcha_single_use(client):
    """验证码一次性使用：同一 captcha_id 第二次校验必须失败。"""
    from bdp.security import captcha as cap_mod

    cap_id, svg = cap_mod.generate()
    assert svg.startswith("<svg")
    answer = cap_mod._unsign(cap_id).split("|")[-2]
    cap_mod.verify(cap_id, answer)  # 第一次通过
    with pytest.raises(cap_mod.CaptchaError):
        cap_mod.verify(cap_id, answer)  # 重放被拒


def test_session_cookie_and_csrf(client):
    """登录签发 HttpOnly 会话 Cookie；Cookie 会话的状态变更需 CSRF 双提交头。"""
    res = client.post("/v1/auth/token", json={"username": "nova", "password": "nova123"})
    assert res.status_code == 200
    set_cookie = res.headers.get("set-cookie", "")
    assert "bdp_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite" in set_cookie
    csrf = res.json()["csrf_token"]

    # 纯 Cookie（无 Bearer）+ 无 CSRF 头 → 403
    client.cookies.set("bdp_session", res.json()["access_token"])
    client.cookies.set("bdp_csrf", csrf)
    no_csrf = client.post("/v1/auth/change-password",
                          json={"old_password": "nova123", "new_password": "nova2026abc"})
    assert no_csrf.status_code == 403

    # 带匹配 CSRF 头 → 通过鉴权（改密成功或旧密码错误均为 4xx 业务码，非 403）
    with_csrf = client.post("/v1/auth/change-password",
                            headers={"X-CSRF-Token": csrf},
                            json={"old_password": "wrong", "new_password": "nova2026abc"})
    assert with_csrf.status_code != 403

    # 登出清除会话
    out = client.post("/v1/auth/logout", headers={"X-CSRF-Token": csrf})
    assert out.status_code == 200
    client.cookies.clear()


def test_password_hash_upgraded_on_login(client):
    """存量弱哈希在登录成功后透明升级到当前迭代次数。"""
    from bdp.db import session_scope
    from bdp.models import AppUser
    from bdp.security.auth import needs_rehash

    # 人为把 nova 的哈希降级为低迭代版本（模拟历史存量）
    with session_scope() as session:
        user = session.get(AppUser, "UB001")
        plain = "nova123"
        user.password_hash = "pbkdf2_sha256$1000$" + user.password_hash.split("$")[2] + "$" + \
            __import__("hashlib").pbkdf2_hmac("sha256", plain.encode(),
                                              bytes.fromhex(user.password_hash.split("$")[2]), 1000).hex()
    assert needs_rehash(user.password_hash)

    res = client.post("/v1/auth/token", json={"username": "nova", "password": plain})
    assert res.status_code == 200  # 旧哈希仍可验证
    with session_scope() as session:
        upgraded = session.get(AppUser, "UB001")
        assert not needs_rehash(upgraded.password_hash)  # 已透明升级


def test_change_password_invalidates_old_token(client, tokens):
    # 用 aurora 的旧 token 改密
    res = client.post(
        "/v1/auth/change-password",
        headers=auth(tokens["aurora"]),
        json={"old_password": "aurora123", "new_password": "aurora2026abc"},
    )
    assert res.status_code == 200
    assert res.json()["token_version"] >= 2

    # 旧 token 立即失效
    old = client.get("/v1/auth/me", headers=auth(tokens["aurora"]))
    assert old.status_code == 401
    assert "失效" in old.json()["message"]

    # 新密码可登录
    login = client.post("/v1/auth/token", json={"username": "aurora", "password": "aurora2026abc"})
    assert login.status_code == 200
    new_tok = login.json()["access_token"]
    assert client.get("/v1/auth/me", headers=auth(new_tok)).status_code == 200

    # 恢复默认密码，避免影响其他用例
    revert = client.post(
        "/v1/auth/change-password",
        headers=auth(new_tok),
        json={"old_password": "aurora2026abc", "new_password": "aurora123"},
    )
    assert revert.status_code == 200


def test_change_password_policy(client, tokens):
    # 错误旧密码
    res = client.post(
        "/v1/auth/change-password",
        headers=auth(tokens["nova"]),
        json={"old_password": "wrong", "new_password": "nova2026abc"},
    )
    assert res.status_code == 400

    # 新密码太短
    res = client.post(
        "/v1/auth/change-password",
        headers=auth(tokens["nova"]),
        json={"old_password": "nova123", "new_password": "a1b2"},
    )
    assert res.status_code == 400

    # 新密码纯数字
    res = client.post(
        "/v1/auth/change-password",
        headers=auth(tokens["nova"]),
        json={"old_password": "nova123", "new_password": "12345678"},
    )
    assert res.status_code == 400

    # 未登录
    res = client.post("/v1/auth/change-password", json={"old_password": "x", "new_password": "x12345678"})
    assert res.status_code == 401


def test_security_gate_blocks_placeholder_secret_in_full_mode(monkeypatch):
    """full 模式 + 占位密钥 → 安全闸门拒绝启动；lite 模式只警告不阻断。"""
    from bdp import bootstrap
    from bdp.config import settings

    monkeypatch.setattr(settings, "jwt_secret", "change-me-in-production-please-use-a-long-random-string")
    monkeypatch.setattr(settings, "mode", "full")
    with pytest.raises(bootstrap.SecurityConfigError):
        bootstrap.enforce_security_gate()

    monkeypatch.setattr(settings, "mode", "lite")
    bootstrap.enforce_security_gate()  # lite 下不抛

    monkeypatch.setattr(settings, "jwt_secret", "x" * 48)
    monkeypatch.setattr(settings, "mode", "full")
    bootstrap.enforce_security_gate()  # 配置了真实密钥后放行


def test_security_gate_catches_env_example_placeholder(monkeypatch):
    """.env.example 的占位串被原样照抄是真实事故路径，闸门必须能挡住它。"""
    from bdp import bootstrap
    from bdp.config import settings

    monkeypatch.setattr(settings, "jwt_secret", "change-me-in-production-please-use-a-long-random-string")
    monkeypatch.setattr(settings, "mode", "full")
    with pytest.raises(bootstrap.SecurityConfigError):
        bootstrap.enforce_security_gate()


def test_cors_disabled_by_default():
    """默认不启用 CORS 中间件（同源托管不需要跨域放行）。"""
    from bdp.api.main import app

    names = [m.cls.__name__ for m in app.user_middleware]
    assert "CORSMiddleware" not in names
