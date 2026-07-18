"""Fallback：调用降级策略。

四种策略：
- ``fail_fast``：直接抛出原始异常（不做任何降级）。
- ``retry``：失败后指数退避 + jitter 重试指定次数。
- ``cache``：缓存成功结果，TTL 内直接返回缓存值。
- ``mock``：直接返回预设的 mock_result（测试 / 降级场景）。

策略通过 ``strategy`` 名称 + 策略映射字典分发（策略模式）。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class FallbackConfig:
    strategy: str = "fail_fast"   # fail_fast / retry / cache / mock
    max_retries: int = 3
    retry_delay: float = 1.0
    retry_backoff: float = 2.0    # 指数退避因子
    cache_ttl: float = 300.0
    mock_result: Any = None
    max_cache_size: int = 128


class Fallback:
    """调用降级执行器。"""

    def __init__(self, config: FallbackConfig | None = None) -> None:
        self._config = config or FallbackConfig()
        # cache: {args_key: (result, expire_at)}
        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()

    @property
    def config(self) -> FallbackConfig:
        return self._config

    async def call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """按配置的策略执行调用。"""
        strategy = self._config.strategy
        if strategy == "fail_fast":
            return await fn(*args, **kwargs)
        if strategy == "mock":
            return self._config.mock_result
        if strategy == "cache":
            return await self._cached_call(fn, *args, **kwargs)
        if strategy == "retry":
            return await self._retry_call(fn, *args, **kwargs)
        raise ValueError(f"unknown fallback strategy: {strategy!r}")

    async def _retry_call(self, fn: Callable, *args, **kwargs) -> Any:
        last_exc: Exception | None = None
        delay = self._config.retry_delay
        for attempt in range(self._config.max_retries + 1):
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                last_exc = e
                if attempt < self._config.max_retries:
                    # jitter: ±25% 随机抖动
                    jitter = delay * random.uniform(0.75, 1.25)
                    await asyncio.sleep(jitter)
                    delay *= self._config.retry_backoff
        raise last_exc  # type: ignore[misc]

    async def _cached_call(self, fn: Callable, *args, **kwargs) -> Any:
        key = self._make_key(fn, args, kwargs)
        now = time.time()
        # 检查缓存是否有效（不删除过期条目，留作 stale fallback）
        stale_result: Any = None
        stale_available = False
        if key in self._cache:
            result, expire_at = self._cache[key]
            if now < expire_at:
                self._cache.move_to_end(key)
                return result
            # 记录过期缓存供 stale fallback
            stale_result = result
            stale_available = True
        try:
            result = await fn(*args, **kwargs)
        except Exception:
            if stale_available:
                return stale_result
            raise
        # 写入缓存
        self._cache[key] = (result, now + self._config.cache_ttl)
        if len(self._cache) > self._config.max_cache_size:
            self._cache.popitem(last=False)
        return result

    def clear_cache(self) -> None:
        """清空所有缓存。"""
        self._cache.clear()

    @staticmethod
    def _make_key(fn: Callable, args: tuple, kwargs: dict) -> str:
        parts = [fn.__name__ if hasattr(fn, "__name__") else str(fn)]
        parts.extend(str(a) for a in args)
        parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
        return "|".join(parts)
