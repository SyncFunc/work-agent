"""Pipeline：韧性层可组合调用链（M3.3 完善）。

执行顺序：RateLimiter → CircuitBreaker → Fallback → 实际调用。

设计要点：
- 限流只作用于入口，重试不重新经过限流器（重试是降级内部逻辑）。
- Fallback 的 retry 在每次重试前检查 CircuitBreaker 状态：如果已 OPEN 则提前放弃重试。
- ``execute_stream`` 保护「创建流」的动作（限流+熔断+retry），
  一旦流开始消费（yield），中途失败不重试——符合 LLM 流式业界共识：
  流建立前可重试，数据开始到达后不重试（已生成的内容不可幂等）。
- ``build_pipeline`` 工厂函数根据 Settings 一键构建 LLM/Sandbox Pipeline。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Callable

from agent.resilience.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitBreakerOpenError, CircuitState
from agent.resilience.fallback import Fallback, FallbackConfig
from agent.resilience.rate_limiter import RateLimiter, RateLimitConfig, RateLimitError


class Pipeline:
    """韧性层可组合调用链。"""

    def __init__(
        self,
        *,
        rate_limiter: RateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        fallback: Fallback | None = None,
        name: str = "",
        rate_limit_key: str = "default",
    ) -> None:
        self._rate_limiter = rate_limiter
        self._circuit_breaker = circuit_breaker
        self._fallback = fallback
        self._name = name
        self._rate_limit_key = rate_limit_key

    @property
    def name(self) -> str:
        return self._name

    async def execute(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """按顺序执行：RateLimiter → CircuitBreaker → Fallback → 实际调用。"""
        # 1. RateLimiter（只检查入口，不检查重试）
        if self._rate_limiter is not None:
            allowed = await self._rate_limiter.acquire(self._rate_limit_key)
            if not allowed:
                if self._fallback is not None:
                    return await self._fallback.call(self._raise_rate_limited)
                raise RateLimitError(
                    f"rate limit exceeded for key '{self._rate_limit_key}'"
                )

        # 2. CircuitBreaker
        if self._circuit_breaker is not None:
            try:
                return await self._circuit_breaker.call(
                    self._call_with_fallback, fn, *args, **kwargs
                )
            except CircuitBreakerOpenError:
                if self._fallback is not None:
                    return await self._fallback.call(self._raise_circuit_open)
                raise

        # 3. 无熔断：直接走 Fallback → 调用
        if self._fallback is not None:
            return await self._fallback.call(fn, *args, **kwargs)
        return await fn(*args, **kwargs)

    async def execute_stream(
        self, factory: Callable, *args: Any, **kwargs: Any
    ) -> AsyncIterator[Any]:
        """带 Pipeline 保护的流式执行。

        保护「创建流」这个动作（限流+熔断+retry），流开始 yield 后不再重试。
        ``factory`` 是一个 async callable，调用后返回 ``AsyncIterator``。
        """
        # 1. RateLimiter（入口限流）
        if self._rate_limiter is not None:
            allowed = await self._rate_limiter.acquire(self._rate_limit_key)
            if not allowed:
                if self._fallback is not None:
                    result = await self._fallback.call(self._raise_rate_limited)
                    if hasattr(result, "__aiter__"):
                        async for item in result:
                            yield item
                    else:
                        # 非迭代器结果（如 mock）直接返回
                        return
                    return
                raise RateLimitError(
                    f"rate limit exceeded for key '{self._rate_limit_key}'"
                )

        # 2. 在 CircuitBreaker + Fallback 保护下创建流
        stream_iter = await self._create_stream_protected(factory, *args, **kwargs)

        # 3. 消费流（不重试）
        if hasattr(stream_iter, "__aiter__"):
            async for item in stream_iter:
                yield item

    async def _create_stream_protected(
        self, factory: Callable, *args: Any, **kwargs: Any
    ) -> Any:
        """在 CircuitBreaker + Fallback 保护下创建流（返回 AsyncIterator）。

        创建失败时可根据 Fallback 策略重试（重新调用 factory）。
        异步生成器是惰性的——body 在 ``async for`` 时才执行。为检测创建失败，
        这里先预读第一项：如果第一项就抛异常，说明创建流失败，可重试。
        预读的第一项通过 ``_prepend`` 放回流中，调用方无感知。
        """

        async def _try_create() -> tuple[Any, AsyncIterator[Any]]:
            """尝试创建流并预读第一项。
            
            返回 (first_item, stream_iterator)，调用方可先 yield first_item
            再继续消费 stream_iterator。
            """
            raw = factory(*args, **kwargs)
            if not hasattr(raw, "__aiter__"):
                raw = await raw  # type: ignore
            stream: AsyncIterator[Any] = raw  # type: ignore
            first_item = await stream.__anext__()
            return first_item, stream

        # 在 CB + Fallback 保护下尝试创建
        if self._circuit_breaker is not None:
            first_item, stream = await self._circuit_breaker.call(
                self._call_with_fallback_for_stream, _try_create
            )
        elif self._fallback is not None:
            first_item, stream = await self._fallback.call(_try_create)
        else:
            first_item, stream = await _try_create()

        # 用 prepend 模式把第一项放回流中
        return _prepend(first_item, stream)

        # 如果 CB/Fallback 抛异常，传到 execute_stream 的调用方

    async def _call_with_fallback_for_stream(self, fn: Callable, *args, **kwargs) -> Any:
        if self._fallback is not None:
            return await self._fallback.call(fn, *args, **kwargs)
        return await fn(*args, **kwargs)

    async def _call_with_fallback(self, fn: Callable, *args, **kwargs) -> Any:
        if self._fallback is not None:
            return await self._fallback.call(fn, *args, **kwargs)
        return await fn(*args, **kwargs)

    @staticmethod
    async def _raise_rate_limited() -> None:
        raise RateLimitError("rate limit exceeded (fallback path)")

    @staticmethod
    async def _raise_circuit_open() -> None:
        raise CircuitBreakerOpenError("circuit breaker is OPEN (fallback path)")


async def _prepend(first: Any, rest: AsyncIterator[Any]) -> AsyncIterator[Any]:
    """把 ``first`` 放回 ``rest`` 流之前。"""
    yield first
    async for item in rest:
        yield item


# --------------------------------------------------------------------------- #
# 工厂函数
# --------------------------------------------------------------------------- #
def build_pipeline(
    *,
    name: str,
    rate_limiter: RateLimiter | None,
    circuit_breaker: CircuitBreaker | None,
    fallback: Fallback | None,
    rate_limit_key: str = "default",
) -> Pipeline | None:
    """构建 Pipeline。若所有组件均为 None 则返回 None（零开销）。"""
    if rate_limiter is None and circuit_breaker is None and fallback is None:
        return None
    return Pipeline(
        rate_limiter=rate_limiter,
        circuit_breaker=circuit_breaker,
        fallback=fallback,
        name=name,
        rate_limit_key=rate_limit_key,
    )


def build_llm_pipeline(settings: Any) -> Pipeline | None:
    """从 Settings 构建 LLM 调用的 Pipeline。"""
    from agent.resilience import (
        CircuitBreaker as _CB,
        CircuitBreakerConfig as _CBCfg,
        Fallback as _FB,
        FallbackConfig as _FBCfg,
        RateLimiter as _RL,
        RateLimitConfig as _RLCfg,
    )

    cfg = settings.resilience
    if not cfg.enabled:
        return None
    rl: RateLimiter | None = None
    cb: CircuitBreaker | None = None
    fb: Fallback | None = None

    rl_cfg = cfg.rate_limit
    if rl_cfg.llm_max_calls > 0:
        rl = _RL(_RLCfg(max_calls=rl_cfg.llm_max_calls, window_seconds=rl_cfg.llm_window_seconds))

    cb_cfg = cfg.circuit_breaker
    if cb_cfg.llm_failure_threshold > 0:
        cb = _CB(_CBCfg(
            failure_threshold=cb_cfg.llm_failure_threshold,
            recovery_timeout=cb_cfg.llm_recovery_timeout,
        ), name="llm")

    fb_cfg = cfg.fallback
    if fb_cfg.llm_strategy != "fail_fast":
        fb = _FB(_FBCfg(
            strategy=fb_cfg.llm_strategy,
            max_retries=3,
            retry_delay=1.0,
            retry_backoff=2.0,
            cache_ttl=300.0,
        ))

    return build_pipeline(
        name="llm",
        rate_limiter=rl,
        circuit_breaker=cb,
        fallback=fb,
        rate_limit_key=f"llm:{settings.llm.model}",
    )


def build_sandbox_pipeline(settings: Any) -> Pipeline | None:
    """从 Settings 构建 Sandbox 调用的 Pipeline。"""
    from agent.resilience import (
        CircuitBreaker as _CB,
        CircuitBreakerConfig as _CBCfg,
        Fallback as _FB,
        FallbackConfig as _FBCfg,
        RateLimiter as _RL,
        RateLimitConfig as _RLCfg,
    )

    cfg = settings.resilience
    if not cfg.enabled:
        return None
    rl: RateLimiter | None = None
    cb: CircuitBreaker | None = None
    fb: Fallback | None = None

    rl_cfg = cfg.rate_limit
    if rl_cfg.sandbox_max_calls > 0:
        rl = _RL(_RLCfg(max_calls=rl_cfg.sandbox_max_calls, window_seconds=rl_cfg.sandbox_window_seconds))

    cb_cfg = cfg.circuit_breaker
    if cb_cfg.sandbox_failure_threshold > 0:
        cb = _CB(_CBCfg(
            failure_threshold=cb_cfg.sandbox_failure_threshold,
            recovery_timeout=cb_cfg.sandbox_recovery_timeout,
        ), name="sandbox")

    fb_cfg = cfg.fallback
    if fb_cfg.sandbox_strategy != "fail_fast":
        fb = _FB(_FBCfg(
            strategy=fb_cfg.sandbox_strategy,
            max_retries=2,
            retry_delay=0.5,
            retry_backoff=2.0,
        ))

    return build_pipeline(
        name="sandbox",
        rate_limiter=rl,
        circuit_breaker=cb,
        fallback=fb,
        rate_limit_key="sandbox:local",
    )
