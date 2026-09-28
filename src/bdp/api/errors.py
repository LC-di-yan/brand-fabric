"""统一错误格式。

现状问题：不同接口出错时，有的返回 `{"detail": ...}`，有的返回自定义结构，
有的直接把 traceback 暴露给前端。企业级系统必须让前端能**靠一个字段**定位错误。

统一为：

```json
{
  "code": "TENANT_VIOLATION",
  "message": "越权尝试：请求租户 T002 与账号租户 T001 不一致",
  "detail": {...},
  "request_id": "req-xxx",
  "path": "/v1/dashboard/summary",
  "ts": "2026-09-26T18:00:00"
}
```

`code` 是机器可读的错误码，前端据此做分支处理（例如 RATE_LIMITED 提示倒计时）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("bdp.error")


class ErrorCode:
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    TENANT_VIOLATION = "TENANT_VIOLATION"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    BAD_REQUEST = "BAD_REQUEST"
    METRIC_ERROR = "METRIC_ERROR"
    SERVER_ERROR = "SERVER_ERROR"
    CREDENTIAL_EXPIRED = "CREDENTIAL_EXPIRED"


_HTTP_TO_CODE = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    429: ErrorCode.RATE_LIMITED,
    422: ErrorCode.VALIDATION_ERROR,
    500: ErrorCode.SERVER_ERROR,
}


@dataclass
class ApiErrorBody:
    code: str
    message: str
    request_id: str = ""
    path: str = ""
    ts: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        body = asdict(self)
        return {k: v for k, v in body.items() if v not in (None, "", {})}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or ""


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class ApiError(Exception):
    """业务层主动抛出的错误：带机器可读的错误码。"""

    def __init__(self, code: str, message: str, status_code: int = 400, detail: dict | None = None) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail or {}
        super().__init__(message)


def build_error_body(
    request: Request, *, code: str, message: str, detail: dict | None = None
) -> dict:
    """构造统一错误体。

    detail 里的字段**平铺到顶层**而不是嵌套在 detail 下，
    这样前端可以稳定地用 `body.retry_after_seconds`、`body.remaining_attempts`
    这类固定路径取值，不需要先判断 detail 的结构。
    """
    body = ApiErrorBody(
        code=code, message=message,
        request_id=_request_id(request), path=request.url.path,
        ts=_ts(), detail=detail or {},
    ).to_dict()
    # 平铺 detail 字段到顶层，但绝不覆盖框架字段
    reserved = set(body.keys())
    for key, value in (detail or {}).items():
        if key not in reserved:
            body[key] = value
    body.pop("detail", None)
    return body


def _http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = _HTTP_TO_CODE.get(exc.status_code, ErrorCode.BAD_REQUEST)
    detail: dict = {}
    message = exc.detail
    # 允许业务层通过 dict 形式传 detail，把 code/message 分开
    if isinstance(exc.detail, dict):
        code = exc.detail.get("code", code)
        message = exc.detail.get("message", str(exc.detail))
        detail = {k: v for k, v in exc.detail.items() if k not in ("code", "message")}
    elif exc.status_code == 403 and isinstance(exc.detail, str) and "越权" in exc.detail:
        code = ErrorCode.TENANT_VIOLATION
    return JSONResponse(
        status_code=exc.status_code,
        content=build_error_body(request, code=code, message=str(message), detail=detail),
    )


def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # 只保留对用户有帮助的字段，不把 pydantic 的完整结构暴露出去
    issues = [
        {"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=build_error_body(
            request, code=ErrorCode.VALIDATION_ERROR,
            message="请求参数不合法", detail={"issues": issues},
        ),
    )


def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=build_error_body(request, code=exc.code, message=exc.message, detail=exc.detail),
    )


def _server_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # 服务器内部错误：只对外暴露错误码与 request_id，不暴露 traceback（防止内部信息泄漏）
    logger.exception("unhandled server error request_id=%s path=%s", _request_id(request), request.url.path)
    return JSONResponse(
        status_code=500,
        content=build_error_body(
            request, code=ErrorCode.SERVER_ERROR,
            message="服务器内部错误，请根据 request_id 联系运维排查",
        ),
    )


def register_error_handlers(app) -> None:
    app.add_exception_handler(HTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(Exception, _server_error_handler)
