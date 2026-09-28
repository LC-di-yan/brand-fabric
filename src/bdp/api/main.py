"""FastAPI 应用入口。

运行：python -m bdp.cli api    或    uvicorn bdp.api.main:app --app-dir src
文档：http://127.0.0.1:8000/docs
看板：http://127.0.0.1:8000/
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from bdp import __version__
from bdp.api.errors import register_error_handlers
from bdp.api.middleware import RequestContextMiddleware, metrics_registry
from bdp.api.routers import admin, agents, auth, catalog, kb, metrics
from bdp.config import settings

STATIC_DIR = Path(__file__).parent / "static"

DESCRIPTION = """
面向多品牌电商代运营场景的多租户数据中台。

**核心设计**
- 多租户隔离：网关解析 `tenant_id` 并强制注入，越权请求 403 + 审计留痕
- 统一指标口径：指标字典支持多口径版本，报表回传口径版本号
- 跨平台商品主数据：归一化 + 规则匹配 + 模糊匹配三级还原
- 知识检索：dense + sparse 双路召回 → RRF 融合 → 重排，租户级隔离

**演示账号**
| 账号 | 密码 | 角色 | 可访问范围 |
|---|---|---|---|
| admin | admin123 | 平台管理员 | 全部租户（需显式指定租户） |
| ops | ops123 | 运营支持 | 跨租户，必须指定 X-Tenant-Id |
| nova / aurora / lumen / verde | 同名+123 | 品牌账号 | 仅本品牌 |
"""

app = FastAPI(
    title="多品牌电商数据中台 API",
    description=DESCRIPTION,
    version=__version__,
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(RequestContextMiddleware)

# CORS 默认关闭：前端由本服务同源托管（/static），不存在跨域场景。
# 需要跨域接入时通过 BDP_CORS_ORIGINS 显式声明来源白名单——
# 带 credentials 的 CORS 不允许 "*"（违反规范且等于不设防），这是刻意的。
if settings.cors_origins:
    from fastapi.middleware.cors import CORSMiddleware

    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        allow_headers=["Authorization", "X-Tenant-Id", "X-CSRF-Token", "X-Request-Id", "Content-Type"],
    )
register_error_handlers(app)

app.include_router(auth.router)
app.include_router(metrics.router)
app.include_router(catalog.router)
app.include_router(kb.router)
app.include_router(admin.router)
app.include_router(agents.router)


@app.on_event("startup")
def _startup_self_check() -> None:
    """启动自检：依赖不可用在第一时间暴露并给出指引。

    full 模式（生产形态）另设安全闸门：密钥是占位值直接拒绝启动，
    避免"忘配密钥的服务跑在公开默认密钥上"这类不可逆事故。
    """
    from bdp.bootstrap import run_self_check

    if settings.mode == "full":
        from bdp.bootstrap import enforce_security_gate

        enforce_security_gate()

    report = run_self_check()
    if not report.ok:
        # 不中断启动（开发环境常见临时依赖未就绪），但把失败打到日志里，便于定位
        import logging

        logging.getLogger("bdp.bootstrap").warning(
            "启动自检未全部通过，服务已启动但部分功能可能异常。详见 /v1/admin/selfcheck。"
        )


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> PlainTextResponse:
    """Prometheus 抓取的指标端点。"""
    return PlainTextResponse(metrics_registry.render(), media_type="text/plain; version=0.0.4")


@app.get("/health", include_in_schema=False)
def liveness() -> dict:
    """存活探针（无需鉴权）。

    与 `/v1/admin/health` 的分工：
    这里是**存活探针**，只回答"进程还在不在、能不能收请求"，供容器/K8s 的 livenessProbe
    使用，因此不做鉴权、不查数据库——探测本身不应该有副作用与依赖。
    详细的组件级健康（数据库 / 向量库 / embedding / 密钥）在 `/v1/admin/selfcheck`，需要鉴权。
    """
    return {"status": "ok", "version": __version__, "mode": settings.mode}


@app.get("/ready", include_in_schema=False)
def readiness() -> dict:
    """就绪探针：确认关键依赖可用后再接流量。"""
    from bdp.bootstrap import run_self_check

    report = run_self_check()
    return {"status": "ready" if report.ok else "degraded", "checks": report.to_dict()["items"]}


@app.get("/", include_in_schema=False)
def login_page() -> FileResponse:
    """登录页。"""
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/dashboard", include_in_schema=False)
def dashboard() -> FileResponse:
    """经营看板（应用外壳）。"""
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/api", include_in_schema=False)
def api_info() -> dict:
    return {
        "name": "多品牌电商数据中台 API",
        "version": __version__,
        "mode": settings.mode,
        "docs": "/docs",
        "dashboard": "/",
        "endpoints": [
            "POST /v1/auth/token",
            "GET  /v1/auth/me",
            "POST /v1/auth/change-password",
            "GET  /v1/metrics",
            "GET  /v1/metrics/{code}/versions",
            "POST /v1/metrics/query  （支持同比/环比/维度下钻）",
            "GET  /v1/metrics/export  （CSV 导出）",
            "GET  /v1/dashboard/summary",
            "GET  /v1/catalog/tenants",
            "GET  /v1/catalog/shops",
            "GET  /v1/catalog/products",
            "GET  /v1/catalog/mapping/coverage",
            "GET  /v1/catalog/mapping/review",
            "POST /v1/catalog/mapping/verify",
            "POST /v1/kb/search",
            "GET  /v1/kb/collections",
            "GET  /v1/kb/documents",
            "POST /v1/kb/documents",
            "PUT  /v1/kb/documents/{doc_id}",
            "DELETE /v1/kb/documents/{doc_id}",
            "GET  /v1/kb/documents/{doc_id}/chunks",
            "POST /v1/kb/rebuild",
            "GET  /v1/admin/health",
            "GET  /v1/admin/selfcheck",
            "GET  /v1/admin/audit",
            "GET  /v1/admin/lineage         （全链路数据血缘）",
            "GET  /v1/admin/lineage/tables  （表级血缘）",
            "GET  /v1/admin/lineage/metrics （指标级血缘）",
            "GET  /v1/admin/data-quality",
            "GET  /v1/admin/data-quality/{rule_id}/samples",
            "POST /v1/agent/runs     （触发 DAG 运行）",
            "GET  /v1/agent/runs     （运行列表）",
            "GET  /v1/agent/runs/{id}（任务明细 + artifacts）",
            "GET  /v1/agent/runs/{id}/events",
            "POST /v1/agent/tasks/{id}/retry",
            "GET  /v1/agent/capabilities",
            "POST /v1/agent/ask      （业务问答，InsightAgent）",
            "GET  /metrics  （Prometheus）",
            "GET  /health   （存活探针，无需鉴权）",
            "GET  /ready    （就绪探针，检查依赖）",
        ],
    }


# 前端静态资源兜底：挂在路由表最末尾，只接住未被上方路由匹配的路径
# （/css/*、/js/*、/vendor/*、favicon 等）。dashboard.html 与 login.html 内
# 使用相对路径引用资源，因此 /dashboard 路由与"直接双击 HTML 文件"两种方式
# 都能加载到样式与脚本；未匹配的 /v1/* 仍走统一错误格式（404 由异常处理器接管）。
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
