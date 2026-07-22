"""CircuitBreaker：经典三态熔断器（CLOSED → OPEN → HALF_OPEN）。

- ``call(fn, *args, **kwargs)``：带熔断保护的调用。
- CLOSED：正常调用，失败计数累加。达阈值 → OPEN。
- OPEN：直接抛 ``CircuitBreakerOpenError``。
- HALF_OPEN：放行一次探测请求，成功 → CLOSED，失败 → OPEN。
- 线程安全：``asyncio.Lock`` 保护状态切换。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(Exception):
    """熔断器 OPEN 状态时抛出的异常。"""


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5  # 连续失败次数阈值
    recovery_timeout: float = 30.0  # OPEN→HALF_OPEN 等待秒数
    half_open_max_calls: int = 1  # HALF_OPEN 状态允许的探测请求数


class CircuitBreaker:
    """三态熔断器。"""

    def __init__(self, config: CircuitBreakerConfig | None = None, *, name: str = "") -> None:
        self._config = config or CircuitBreakerConfig()
        self._name = name
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._name

    def state(self) -> CircuitState:
        return self._state

    def failure_count(self) -> int:
        return self._failure_count

    async def call(self, fn, *args, **kwargs):
        """带熔断保护的调用。OPEN 时直接抛 ``CircuitBreakerOpenError``。"""
        async with self._lock:
            if self._state == CircuitState.OPEN:
                if time.time() - self._last_failure_time >= self._config.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                else:
                    raise CircuitBreakerOpenError(
                        f"circuit breaker '{self._name}' is OPEN; "
                        f"retry after {self._config.recovery_timeout - (time.time() - self._last_failure_time):.1f}s"
                    )

            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self._config.half_open_max_calls:
                    raise CircuitBreakerOpenError(
                        f"circuit breaker '{self._name}' is HALF_OPEN; "
                        f"max probe calls ({self._config.half_open_max_calls}) reached"
                    )
                self._half_open_calls += 1

        # 释放锁后执行实际调用（避免持有锁期间做 IO）
        try:
            result = await fn(*args, **kwargs)
        except Exception:
            async with self._lock:
                self._do_record_failure()
            raise

        async with self._lock:
            self._do_record_success()
        return result

    async def record_success(self) -> None:
        """手动记录成功（用于无需 call() 包装的场景）。"""
        async with self._lock:
            self._do_record_success()

    async def record_failure(self) -> None:
        """手动记录失败。"""
        async with self._lock:
            self._do_record_failure()

    def _do_record_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
        elif self._state == CircuitState.CLOSED:
            self._failure_count = 0

    def _do_record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
        elif (
            self._state == CircuitState.CLOSED
            and self._failure_count >= self._config.failure_threshold
        ):
            self._state = CircuitState.OPEN
