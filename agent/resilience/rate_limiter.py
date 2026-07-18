"""RateLimiter：令牌桶滑动窗口限流器。

基于 ``collections.deque[float]`` 存储窗口内请求时间戳，非阻塞纯异步。
``acquire()`` 立即返回布尔值（不等待窗口滑动）。
不同 ``key`` 相互隔离，支持按维度限流（如 LLM / Sandbox 分别计数）。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


class RateLimitError(Exception):
    """被限流时抛出。"""


@dataclass
class RateLimitConfig:
    max_calls: int = 60          # 窗口内最大调用次数
    window_seconds: int = 60     # 滑动窗口秒数


class RateLimiter:
    """滑动窗口限流器。``key`` 参数支持多维度隔离。"""

    def __init__(self, config: RateLimitConfig | None = None) -> None:
        self._config = config or RateLimitConfig()
        # key -> deque[timestamp]
        self._windows: dict[str, deque[float]] = {}

    async def acquire(self, key: str = "default") -> bool:
        """尝试获取一个令牌；返回 True 成功 / False 被限流。"""
        now = time.time()
        window = self._get_window(key)
        self._slide(window, now)
        if len(window) >= self._config.max_calls:
            return False
        window.append(now)
        return True

    def remaining(self, key: str = "default") -> float:
        """当前 key 在窗口内剩余配额（0~1 之间的浮点数）。"""
        now = time.time()
        window = self._get_window(key)
        self._slide(window, now)
        return max(0.0, 1.0 - len(window) / self._config.max_calls)

    def reset(self) -> None:
        """重置所有 key 的状态。"""
        self._windows.clear()

    def _get_window(self, key: str) -> deque[float]:
        if key not in self._windows:
            self._windows[key] = deque()
        return self._windows[key]

    def _slide(self, window: deque[float], now: float) -> None:
        """移除窗口外的过期时间戳。"""
        cutoff = now - self._config.window_seconds
        while window and window[0] < cutoff:
            window.popleft()
