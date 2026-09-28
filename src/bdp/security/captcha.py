"""登录验证码：无状态 HMAC 签名 + SVG 渲染（零依赖，纯标准库）。

设计
----
- 服务端生成算术题（如 7 + 3），把 {答案, 过期时间} 用 JWT 密钥签名成 captcha_id
  返回给前端；服务端**不存任何会话状态**——与项目"多实例部署换 Redis"的
  既有取舍一致，签名方案天然支持水平扩展。
- 渲染为 SVG：数字用随机旋转/位移/干扰线，防简单 OCR；SVG 是文本，无需图像库。
- 防重放：已验证通过的 captcha_id 进入进程内 TTL 集合（一次性使用）。
  多实例场景应换 Redis SETNX，与限流器的部署说明一致。
- 有效期默认 5 分钟（BDP_CAPTCHA_TTL_SECONDS）。

安全边界
--------
验证码只提高自动化攻击成本，不替代限流与锁定；两者串联使用：
失败 3 次 → 要求验证码；失败 5 次 → 锁定 15 分钟。
"""

from __future__ import annotations

import hmac
import hashlib
import random
import re
import secrets
import threading
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode

from bdp.config import settings

_TTL_SECONDS = settings.captcha_ttl_seconds
_FONT = 'font-family="Segoe UI, Arial, sans-serif"'


class CaptchaError(ValueError):
    """验证码无效 / 过期 / 答案错误（统一对外文案，不区分原因，避免探测）。"""


# ---- 一次性使用记录（进程内；多实例换 Redis） -----------------------------

_used_lock = threading.Lock()
_used: dict[str, float] = {}  # captcha_id -> 过期时间戳


def _prune_used(now: float) -> None:
    for key in [k for k, exp in _used.items() if exp <= now]:
        _used.pop(key, None)


# ---- 签名 / 验签 -----------------------------------------------------------

def _sign(payload: str) -> str:
    sig = hmac.new(settings.jwt_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    token = urlsafe_b64encode(f"{payload}|{sig}".encode()).decode().rstrip("=")
    return token


def _unsign(token: str) -> str | None:
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = urlsafe_b64decode(padded.encode()).decode()
        payload, sig = raw.rsplit("|", 1)
        expected = hmac.new(settings.jwt_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(sig, expected):
            return None
        return payload
    except (ValueError, UnicodeDecodeError):
        return None


# ---- 生成 / 校验 -----------------------------------------------------------

def generate() -> tuple[str, str]:
    """返回 (captcha_id, svg)。captcha_id 自含答案与过期时间（签名保护）。"""
    rng = random.Random(secrets.randbits(64))
    a, b = rng.randint(1, 9), rng.randint(1, 9)
    if rng.random() < 0.5:
        expr, answer = f"{a} + {b}", a + b
    else:
        a, b = max(a, b), min(a, b)
        expr, answer = f"{a} - {b}", a - b
    expires_at = int(time.time()) + _TTL_SECONDS
    captcha_id = _sign(f"{answer}|{expires_at}")
    return captcha_id, _render_svg(expr, rng)


def verify(captcha_id: str, code: str) -> None:
    """校验失败抛 CaptchaError；成功则标记一次性使用。"""
    payload = _unsign(captcha_id or "")
    if not payload:
        raise CaptchaError("验证码无效")
    try:
        answer_s, expires_s = payload.split("|")
        answer, expires_at = int(answer_s), int(expires_s)
    except ValueError:
        raise CaptchaError("验证码无效") from None
    if time.time() > expires_at:
        raise CaptchaError("验证码已过期，请点击刷新")
    now = time.time()
    with _used_lock:
        _prune_used(now)
        if captcha_id in _used:
            raise CaptchaError("验证码已使用，请刷新")
        if not re.fullmatch(r"-?\d{1,3}", (code or "").strip()):
            raise CaptchaError("验证码答案格式不正确")
        if int(code.strip()) != answer:
            raise CaptchaError("验证码答案错误")
        _used[captcha_id] = now + _TTL_SECONDS


# ---- SVG 渲染 --------------------------------------------------------------

def _render_svg(expr: str, rng: random.Random) -> str:
    """数字逐字随机旋转/抖动 + 干扰线/点，提高机器识别成本。"""
    w, h = 132, 44
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="验证码">',
        f'<rect width="{w}" height="{h}" rx="6" fill="#f4f8fd"/>',
    ]
    # 干扰线
    for _ in range(3):
        x1, y1 = rng.randint(0, w), rng.randint(0, h)
        x2, y2 = rng.randint(0, w), rng.randint(0, h)
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#b9d4ee" stroke-width="1.2"/>')
    # 逐字符渲染
    x = 12
    for ch in expr:
        if ch == " ":
            x += 6
            continue
        if ch in "+-":
            parts.append(
                f'<text x="{x}" y="{rng.randint(28, 34)}" fill="#5b7a9d" font-size="17" {_FONT}>{ch}</text>'
            )
            x += 18
            continue
        rot = rng.randint(-14, 14)
        dy = rng.randint(-3, 3)
        parts.append(
            f'<text x="{x}" y="{29 + dy}" fill="#0d3b66" font-size="21" font-weight="600" '
            f'letter-spacing="1" transform="rotate({rot} {x + 6} 26)" {_FONT}>{ch}</text>'
        )
        x += 21
    # 干扰点
    for _ in range(14):
        parts.append(f'<circle cx="{rng.randint(2, w - 2)}" cy="{rng.randint(2, h - 2)}" r="1" fill="#9dbfe2"/>')
    parts.append("</svg>")
    return "".join(parts)
