"""请求上下文中间件 + Prometheus 指标。

两件事：
1. **请求 ID 与访问日志**：每个请求分配 request_id，打访问日志（方法/路径/状态/耗时），
   错误处理时用同一个 request_id 关联，排查不用猜。
2. **Prometheus 指标**：请求数、耗时直方图，供 `/metrics` 端点暴露。

为什么自研而不用 prometheus-client
------------------------------------
本项目的指标只有"请求数 + 耗时"两类，prometheus-client 的完整模型（多进程共享
collector、推送网关）在这里是过度依赖。自研几十行即可满足，且便于在日志里解释每个指标。
生产多实例部署时应替换为 prometheus-client + 共享 registry。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("bdp.access")

HISTOGRAM_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# 不纳入统计的路径（避免监控数据被自监控污染）
_SKIP_PREFIXES = ("/metrics", "/favicon.ico")


class MetricsRegistry:
    """进程内指标注册表（线程安全）。"""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self.requests_total: dict[tuple[str, str, int], int] = defaultdict(int)
        self.duration_buckets: dict[tuple[str, str], list[int]] = {}
        self.duration_sum: dict[tuple[str, str], float] = defaultdict(float)
        self.duration_count: dict[tuple[str, str], int] = defaultdict(int)
        # ---- agent 任务指标（P0 可观测：任务终态计数 + 执行耗时 + 重试）----
        self.agent_task_total: dict[tuple[str, str], int] = defaultdict(int)
        self.agent_duration_sum: dict[str, float] = defaultdict(float)
        self.agent_duration_count: dict[str, int] = defaultdict(int)
        self.agent_retries_total: dict[str, int] = defaultdict(int)
        self.agent_runs_active = 0

    # -- agent 指标（由 orchestrator/worker 在任务终态时调用）----------------

    def observe_agent_task(self, agent: str, status: str, duration_sec: float,
                           *, retried: bool = False) -> None:
        with self._mu:
            self.agent_task_total[(agent, status)] += 1
            self.agent_duration_sum[agent] += duration_sec
            self.agent_duration_count[agent] += 1
            if retried:
                self.agent_retries_total[agent] += 1

    def set_active_runs(self, n: int) -> None:
        with self._mu:
            self.agent_runs_active = max(n, 0)

    def observe(self, method: str, route: str, status: int, duration_seconds: float) -> None:
        with self._mu:
            self.requests_total[(method, route, status)] += 1
            key = (method, route)
            if key not in self.duration_buckets:
                self.duration_buckets[key] = [0] * (len(HISTOGRAM_BUCKETS) + 1)
            bucket = self.duration_buckets[key]
            for i, bound in enumerate(HISTOGRAM_BUCKETS):
                if duration_seconds <= bound:
                    bucket[i] += 1
            bucket[-1] += 1  # +Inf
            self.duration_sum[key] += duration_seconds
            self.duration_count[key] += 1

    def render(self) -> str:
        with self._mu:
            lines = [
                "# HELP bdp_http_requests_total HTTP 请求总数",
                "# TYPE bdp_http_requests_total counter",
            ]
            for (method, route, status), count in sorted(self.requests_total.items()):
                lines.append(
                    f'bdp_http_requests_total{{method="{method}",route="{route}",status="{status}"}} {count}'
                )

            lines.append("# HELP bdp_http_request_duration_seconds HTTP 请求耗时直方图")
            lines.append("# TYPE bdp_http_request_duration_seconds histogram")
            for (method, route), buckets in sorted(self.duration_buckets.items()):
                for i, bound in enumerate(HISTOGRAM_BUCKETS):
                    lines.append(
                        f'bdp_http_request_duration_seconds_bucket{{method="{method}",route="{route}",le="{bound}"}} {buckets[i]}'
                    )
                lines.append(
                    f'bdp_http_request_duration_seconds_bucket{{method="{method}",route="{route}",le="+Inf"}} {buckets[-1]}'
                )
                lines.append(
                    f'bdp_http_request_duration_seconds_sum{{method="{method}",route="{route}"}} {self.duration_sum[(method, route)]:.6f}'
                )
                lines.append(
                    f'bdp_http_request_duration_seconds_count{{method="{method}",route="{route}"}} {self.duration_count[(method, route)]}'
                )

            # ---- agent 任务指标 ----
            lines.append("# HELP bdp_agent_task_total Agent 任务终态计数")
            lines.append("# TYPE bdp_agent_task_total counter")
            for (agent, status), count in sorted(self.agent_task_total.items()):
                lines.append(f'bdp_agent_task_total{{agent="{agent}",status="{status}"}} {count}')

            lines.append("# HELP bdp_agent_task_duration_seconds Agent 任务累计执行耗时")
            lines.append("# TYPE bdp_agent_task_duration_seconds counter")
            for agent, total in sorted(self.agent_duration_sum.items()):
                count = self.agent_duration_count.get(agent, 0)
                lines.append(f'bdp_agent_task_duration_seconds_sum{{agent="{agent}"}} {total:.6f}')
                lines.append(f'bdp_agent_task_duration_seconds_count{{agent="{agent}"}} {count}')

            lines.append("# HELP bdp_agent_task_retries_total Agent 任务重试计数")
            lines.append("# TYPE bdp_agent_task_retries_total counter")
            for agent, count in sorted(self.agent_retries_total.items()):
                lines.append(f'bdp_agent_task_retries_total{{agent="{agent}"}} {count}')

            lines.append("# HELP bdp_agent_runs_active 当前 running 状态的运行数")
            lines.append("# TYPE bdp_agent_runs_active gauge")
            lines.append(f"bdp_agent_runs_active {self.agent_runs_active}")

            lines.append("")
            return "\n".join(lines)


metrics_registry = MetricsRegistry()


class RequestContextMiddleware(BaseHTTPMiddleware):
    """分配 request_id、计时、打访问日志、记录指标。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-Id") or f"req-{uuid.uuid4().hex[:12]}"
        request.state.request_id = request_id

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = time.perf_counter() - start
            self._record(request, 500, duration)
            logger.error(
                "request_id=%s %s %s 500 %.3fs (unhandled)", request_id,
                request.method, request.url.path, duration,
            )
            raise

        duration = time.perf_counter() - start
        status = response.status_code
        self._record(request, status, duration)

        # 4xx/5xx 打 warning，正常请求打 info；避免健康检查刷屏
        level = logging.WARNING if status >= 400 else logging.INFO
        if not request.url.path.startswith(_SKIP_PREFIXES):
            logger.log(
                level, "request_id=%s %s %s -> %d %.3fs",
                request_id, request.method, request.url.path, status, duration,
            )

        response.headers["X-Request-Id"] = request_id
        return response

    @staticmethod
    def _record(request: Request, status: int, duration: float) -> None:
        if request.url.path.startswith(_SKIP_PREFIXES):
            return
        metrics_registry.observe(request.method, request.url.path, status, duration)
