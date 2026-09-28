"""LLM 客户端（OpenAI 兼容协议，可选启用）。

与 embedding 后端同一套取舍：默认 none（不依赖任何外部服务，InsightAgent 走
确定性降级路径）；配置 BDP_LLM_BACKEND=openai + BDP_LLM_API_BASE 后启用。
HTTP 客户端复用已在依赖清单中的 httpx，零新增依赖。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from bdp.config import settings

logger = logging.getLogger("bdp.llm")


class LLMUnavailable(Exception):
    """LLM 后端未配置或调用失败——调用方（InsightAgent）据此走降级路径。"""


class LLMClient:
    """最小化的 chat/completions 客户端：只封装 InsightAgent 需要的部分。"""

    name = "unset"

    def __init__(self) -> None:
        if settings.llm_backend == "none":
            raise LLMUnavailable("LLM 后端未启用（BDP_LLM_BACKEND=none）")
        if not settings.llm_api_base:
            raise LLMUnavailable("未配置 BDP_LLM_API_BASE")
        self.name = settings.llm_model or "unknown"
        self._base = settings.llm_api_base.rstrip("/")

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> dict:
        """返回 assistant 消息（含 tool_calls 时由调用方继续循环）。"""
        import httpx  # 局部导入，避免无网络场景的启动开销

        payload: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            resp = httpx.post(
                f"{self._base}/v1/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=settings.llm_timeout_sec,
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("LLM 调用失败：%s", exc)
            raise LLMUnavailable(f"LLM 调用失败：{exc}") from exc

        data = resp.json()
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMUnavailable(f"LLM 响应格式异常：{data}") from exc

    @staticmethod
    def _headers() -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        return headers


def tool_call_arguments(message: dict) -> list[tuple[str, str, dict]]:
    """从 assistant 消息提取 (call_id, tool_name, args)；参数非法时按空参处理。"""
    out: list[tuple[str, str, dict]] = []
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        out.append((call.get("id", ""), fn.get("name", ""), args))
    return out
