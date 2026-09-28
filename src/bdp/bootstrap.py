"""启动自检（Bootstrap Self-Check）。

企业级系统不应"起来了才发现连不上库"。启动时把配置与依赖逐条校验，
问题在第一时间暴露并给出明确指引，而不是在某个接口里报一个含糊的 500。

用法：
- 应用启动时自动执行（FastAPI startup 事件），把结果打到日志
- `GET /v1/admin/selfcheck` 随时可查
- `python -m bdp.cli doctor` 命令行预检（部署前手动跑一次）
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import text

from bdp.config import settings

logger = logging.getLogger("bdp.bootstrap")

# 已知的"占位密钥"黑名单：配置成这些值视同未配置。
# config.py 的开发默认值与 .env.example 的占位串都要挡住——
# 只挡前者会漏掉"照抄 .env.example 没改"这一最常见的真实事故路径。
_KNOWN_PLACEHOLDER_SECRETS = frozenset({
    "dev-only-secret-please-override-with-a-long-random-string",       # config.py 默认值
    "change-me-in-production-please-use-a-long-random-string",          # .env.example 占位值
})


@dataclass
class CheckItem:
    name: str
    ok: bool
    latency_ms: float
    detail: str = ""
    fix_hint: str = ""


@dataclass
class SelfCheckReport:
    ok: bool
    items: list[CheckItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "mode": settings.mode,
            "database": "sqlite" if settings.is_sqlite else "postgresql",
            "vector_backend": settings.vector_backend,
            "embedding_backend": settings.embedding_backend,
            "items": [
                {
                    "name": i.name, "ok": i.ok, "latency_ms": round(i.latency_ms, 1),
                    "detail": i.detail, "fix_hint": i.fix_hint,
                }
                for i in self.items
            ],
        }


def _timed(fn) -> tuple[bool, float, str]:
    start = time.perf_counter()
    try:
        detail = fn()
        return True, (time.perf_counter() - start) * 1000, str(detail)
    except Exception as exc:  # pragma: no cover - 依赖具体环境
        return False, (time.perf_counter() - start) * 1000, f"{type(exc).__name__}: {exc}"


def check_database() -> CheckItem:
    def _check() -> str:
        from bdp.db import session_scope

        with session_scope() as session:
            v = session.execute(text("SELECT 1")).scalar()
            return f"连接正常，SELECT 1 = {v}"

    ok, ms, detail = _timed(_check)
    return CheckItem(
        "database", ok, ms, detail,
        fix_hint="检查 BDP_DATABASE_URL 是否正确；full 模式需先 docker compose up -d postgres",
    )


def check_vector_store() -> CheckItem:
    def _check() -> str:
        if settings.vector_backend == "milvus":
            from bdp.kb.store import MilvusVectorStore

            store = MilvusVectorStore()
            cols = store.health()
            if not cols.get("ok"):
                raise RuntimeError(cols.get("error", "无法连接 Milvus"))
            return f"连接正常，现有 collection {len(cols.get('collections') or [])} 个"
        from bdp.kb.store import LocalVectorStore

        store = LocalVectorStore()
        return f"本地向量库就绪，目录 {store.path}"

    ok, ms, detail = _timed(_check)
    return CheckItem(
        "vector_store", ok, ms, detail,
        fix_hint="Milvus 未就绪时首次需 60-90 秒等待 healthcheck；也可临时切 BDP_VECTOR_BACKEND=local",
    )


def check_embedding() -> CheckItem:
    def _check() -> str:
        from bdp.kb.embedding import build_embedder

        emb = build_embedder()
        vec = emb.encode(["自检"])
        return f"{emb.name} 就绪，向量维度 {vec.shape[1]}"

    ok, ms, detail = _timed(_check)
    return CheckItem(
        "embedding", ok, ms, detail,
        fix_hint="BGE/OpenAI 后端需联网或本地模型；离线环境请用 BDP_EMBEDDING_BACKEND=hash",
    )


def check_security_config() -> CheckItem:
    warnings = []
    if settings.jwt_secret in _KNOWN_PLACEHOLDER_SECRETS:
        warnings.append("JWT 密钥仍是占位值（视同未配置）")
    if len(settings.jwt_secret) < 32:
        warnings.append("JWT 密钥长度不足 32 字节")
    ok = not warnings
    return CheckItem(
        "security_config", ok, 0.0,
        detail="；".join(warnings) if warnings else "密钥配置正常",
        fix_hint="设置足够长的 BDP_JWT_SECRET（≥ 32 字节随机串），例如 python -c \"import secrets;print(secrets.token_urlsafe(48))\"",
    )


class SecurityConfigError(RuntimeError):
    """full 模式下密钥配置不安全，拒绝启动。"""


def enforce_security_gate() -> None:
    """生产形态（full 模式）的启动闸门：密钥是占位值直接拒绝启动。

    lite 演示模式保持宽松（warning 不阻断），full 模式面向真实部署，
    带 12 行代码换"忘配密钥的服务跑在默认密钥上"这个不可逆事故的免疫。
    """
    item = check_security_config()
    if settings.mode == "full" and not item.ok:
        raise SecurityConfigError(
            f"full 模式安全闸门未通过：{item.detail}。修复：{item.fix_hint}"
        )


def run_self_check() -> SelfCheckReport:
    items = [
        check_database(),
        check_vector_store(),
        check_embedding(),
        check_security_config(),
    ]
    report = SelfCheckReport(ok=all(i.ok for i in items), items=items)

    for i in items:
        level = logging.INFO if i.ok else logging.ERROR
        logger.log(
            level, "selfcheck %s %s (%.0fms) %s",
            "PASS" if i.ok else "FAIL", i.name, i.latency_ms, i.detail or i.fix_hint,
        )
    if not report.ok:
        failed = [i.name for i in items if not i.ok]
        logger.error("自检未通过：%s。请先解决上述问题再启动服务。", ", ".join(failed))
    return report
