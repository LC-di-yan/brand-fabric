"""登录限流：防止暴力破解。

企业级系统必备。规则：
- 同一账号连续失败 5 次，锁定 15 分钟
- 锁定期间即使密码正确也拒绝
- 返回剩余尝试次数，便于前端提示
- 进程内存实现（多实例部署时应替换为 Redis，README 已注明）

为什么不用第三方库：限流逻辑简单（计数 + 过期），自研比引入依赖更轻，
且便于在日志里输出明确的判定过程，便于运维排查。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


class RateLimited(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"尝试次数过多，已锁定 {retry_after_seconds} 秒")


@dataclass
class _Entry:
    failures: int = 0
    locked_until: float = 0.0


class LoginRateLimiter:
    def __init__(self, max_failures: int = 5, lock_seconds: int = 900) -> None:
        self._max = max_failures
        self._lock_seconds = lock_seconds
        self._entries: dict[str, _Entry] = {}
        self._mu = threading.Lock()

    def _entry(self, key: str) -> _Entry:
        return self._entries.setdefault(key, _Entry())

    def _prune(self) -> None:
        # 惰性清理：避免内存无限增长。生产多实例应用 Redis 带 TTL。
        now = time.monotonic()
        stale = [k for k, v in self._entries.items() if v.failures == 0 and now >= v.locked_until]
        for k in stale:
            self._entries.pop(k, None)

    def remaining_attempts(self, key: str) -> int:
        with self._mu:
            entry = self._entry(key)
            return max(0, self._max - entry.failures)

    def failures(self, key: str) -> int:
        """当前连续失败计数（用于决定是否需要验证码）。"""
        with self._mu:
            return self._entry(key).failures

    def lock_remaining_seconds(self, key: str) -> int:
        with self._mu:
            entry = self._entry(key)
            return max(0, int(entry.locked_until - time.monotonic()))

    def check(self, key: str) -> None:
        """调用前校验：处于锁定状态则抛出 RateLimited。"""
        with self._mu:
            entry = self._entry(key)
            remaining = int(entry.locked_until - time.monotonic())
            if remaining > 0:
                raise RateLimited(remaining)

    def record_failure(self, key: str) -> int:
        """记录一次失败，返回剩余尝试次数。达到上限则锁定（返回 0）。

        语义说明：前 max-1 次失败返回递减的剩余次数，第 max 次失败触发锁定并返回 0，
        此后 check() 会抛出 RateLimited。锁定计数重置，锁定期满后重新计数。
        """
        with self._mu:
            entry = self._entry(key)
            entry.failures += 1
            if entry.failures >= self._max:
                entry.locked_until = time.monotonic() + self._lock_seconds
                entry.failures = 0
                self._prune()
                return 0
            self._prune()
            return max(0, self._max - entry.failures)

    def record_success(self, key: str) -> None:
        with self._mu:
            self._entries.pop(key, None)


# 全局单例：登录限流作用于整个进程（阈值可经 BDP_LOGIN_* 配置）
from bdp.config import settings  # noqa: E402  放在末尾避免与默认参数求值顺序耦合

login_rate_limiter = LoginRateLimiter(
    max_failures=settings.login_max_failures,
    lock_seconds=settings.login_lock_seconds,
)
