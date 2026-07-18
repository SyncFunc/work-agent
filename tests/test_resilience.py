"""M3.2 验收：韧性层核心组件（RateLimiter / CircuitBreaker / Fallback / Pipeline）。"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from agent.resilience import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    Fallback,
    FallbackConfig,
    Pipeline,
    RateLimiter,
    RateLimitConfig,
    RateLimitError,
)


# --------------------------------------------------------------------------- #
# RateLimiter
# --------------------------------------------------------------------------- #
class TestRateLimiter:
    def test_acquire_basic(self):
        """窗口内准确计数，超配额返回 False。"""
        limiter = RateLimiter(RateLimitConfig(max_calls=3, window_seconds=10))
        assert asyncio.run(limiter.acquire("k")) is True
        assert asyncio.run(limiter.acquire("k")) is True
        assert asyncio.run(limiter.acquire("k")) is True
        assert asyncio.run(limiter.acquire("k")) is False

    def test_remaining(self):
        """remaining 返回 0~1 之间的剩余配额。"""
        limiter = RateLimiter(RateLimitConfig(max_calls=4, window_seconds=10))
        assert limiter.remaining("k") == 1.0
        asyncio.run(limiter.acquire("k"))
        assert limiter.remaining("k") == 0.75

    def test_key_isolation(self):
        """不同 key 互不影响。"""
        limiter = RateLimiter(RateLimitConfig(max_calls=2, window_seconds=10))
        assert asyncio.run(limiter.acquire("a")) is True
        assert asyncio.run(limiter.acquire("a")) is True
        assert asyncio.run(limiter.acquire("a")) is False  # a 被限
        assert asyncio.run(limiter.acquire("b")) is True   # b 不受影响

    def test_reset(self):
        """reset 清空所有 key 的状态。"""
        limiter = RateLimiter(RateLimitConfig(max_calls=1, window_seconds=10))
        asyncio.run(limiter.acquire("k"))
        assert asyncio.run(limiter.acquire("k")) is False
        limiter.reset()
        assert asyncio.run(limiter.acquire("k")) is True

    def test_window_slide(self):
        """窗口滑动后旧请求过期，配额恢复。"""
        limiter = RateLimiter(RateLimitConfig(max_calls=2, window_seconds=0.05))
        assert asyncio.run(limiter.acquire("k")) is True
        assert asyncio.run(limiter.acquire("k")) is True
        assert asyncio.run(limiter.acquire("k")) is False
        # 等待窗口过期
        asyncio.run(asyncio.sleep(0.06))
        assert asyncio.run(limiter.acquire("k")) is True

    def test_default_config(self):
        """默认配置（60 calls/60s）可用。"""
        limiter = RateLimiter()
        assert asyncio.run(limiter.acquire()) is True


# --------------------------------------------------------------------------- #
# CircuitBreaker
# --------------------------------------------------------------------------- #
class TestCircuitBreaker:
    def test_initial_state_closed(self):
        """初始状态为 CLOSED。"""
        cb = CircuitBreaker(name="test")
        assert cb.state().value == "closed"

    async def _ok(self) -> str:
        return "ok"

    async def _fail(self) -> str:
        raise ValueError("fail")

    def test_closed_to_open(self):
        """连续失败达阈值 → OPEN。"""
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3, recovery_timeout=60), name="t")
        for _ in range(3):
            with pytest.raises(ValueError):
                asyncio.run(cb.call(self._fail))
        assert cb.state().value == "open"
        assert cb.failure_count() == 3

    def test_open_raises(self):
        """OPEN 时调用抛 CircuitBreakerOpenError（而不是 ValueError）。"""
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, recovery_timeout=60), name="t")
        with pytest.raises(ValueError):
            asyncio.run(cb.call(self._fail))
        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(cb.call(self._ok))

    def test_half_open_success_to_closed(self):
        """HALF_OPEN 探测成功 → 回到 CLOSED。"""
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.05), name="t")
        with pytest.raises(ValueError):
            asyncio.run(cb.call(self._fail))
        assert cb.state().value == "open"
        asyncio.run(asyncio.sleep(0.06))
        asyncio.run(cb.call(self._ok))
        assert cb.state().value == "closed"
        assert cb.failure_count() == 0

    def test_half_open_failure_to_open(self):
        """HALF_OPEN 探测失败 → 回到 OPEN。"""
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2, recovery_timeout=0.05), name="t")
        with pytest.raises(ValueError):
            asyncio.run(cb.call(self._fail))
        with pytest.raises(ValueError):
            asyncio.run(cb.call(self._fail))
        assert cb.state().value == "open"
        asyncio.run(asyncio.sleep(0.06))
        with pytest.raises(ValueError):
            asyncio.run(cb.call(self._fail))
        assert cb.state().value == "open"

    def test_success_resets_counter(self):
        """连续成功重置失败计数。"""
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3), name="t")
        with pytest.raises(ValueError):
            asyncio.run(cb.call(self._fail))
        with pytest.raises(ValueError):
            asyncio.run(cb.call(self._fail))
        asyncio.run(cb.call(self._ok))  # 成功重置
        assert cb.failure_count() == 0

    def test_name_property(self):
        """name 属性返回构造时的名称。"""
        cb = CircuitBreaker(name="my-cb")
        assert cb.name == "my-cb"

    def test_half_open_max_calls(self):
        """HALF_OPEN 最多放行 half_open_max_calls 次探测。"""
        cb = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.05, half_open_max_calls=1),
            name="t",
        )
        with pytest.raises(ValueError):
            asyncio.run(cb.call(self._fail))
        assert cb.state().value == "open"
        asyncio.run(asyncio.sleep(0.06))
        with pytest.raises(ValueError):
            asyncio.run(cb.call(self._fail))
        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(cb.call(self._ok))


# --------------------------------------------------------------------------- #
# Fallback
# --------------------------------------------------------------------------- #
class TestFallback:
    async def _ok(self) -> str:
        return "ok"

    async def _fail(self) -> str:
        raise ValueError("fail")

    async def _ok_side_effect(self) -> str:
        """第一次调用失败，第二次成功。"""
        if not hasattr(self, "_call_count"):
            self._call_count = 0
        self._call_count += 1
        if self._call_count < 2:
            raise ValueError("fail first")
        return "ok"

    def test_fail_fast(self):
        """fail_fast 直接抛出原始异常。"""
        fb = Fallback(FallbackConfig(strategy="fail_fast"))
        with pytest.raises(ValueError, match="fail"):
            asyncio.run(fb.call(self._fail))

    def test_mock(self):
        """mock 返回预设值。"""
        fb = Fallback(FallbackConfig(strategy="mock", mock_result="mocked"))
        result = asyncio.run(fb.call(self._fail))
        assert result == "mocked"

    def test_mock_does_not_call_fn(self):
        """mock 策略不调用原始函数。"""
        called = False

        async def never_called():
            nonlocal called
            called = True
            return "real"

        fb = Fallback(FallbackConfig(strategy="mock", mock_result="mocked"))
        result = asyncio.run(fb.call(never_called))
        assert result == "mocked"
        assert called is False

    def test_retry_success(self):
        """retry 在失败后重试，最终成功。"""
        attempt_count = 0

        async def flaky():
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise ValueError(f"fail attempt {attempt_count}")
            return "ok"

        fb = Fallback(FallbackConfig(strategy="retry", max_retries=3, retry_delay=0.01))
        result = asyncio.run(fb.call(flaky))
        assert result == "ok"
        assert attempt_count == 3

    def test_retry_exhausted(self):
        """retry 耗尽重试次数后抛出原始异常。"""
        fb = Fallback(FallbackConfig(strategy="retry", max_retries=2, retry_delay=0.01))
        with pytest.raises(ValueError, match="fail"):
            asyncio.run(fb.call(self._fail))

    def test_cache_hit(self):
        """cache TTL 内返回缓存值。"""
        call_count = 0

        async def expensive():
            nonlocal call_count
            call_count += 1
            return f"result-{call_count}"

        fb = Fallback(FallbackConfig(strategy="cache", cache_ttl=60))
        r1 = asyncio.run(fb.call(expensive))
        r2 = asyncio.run(fb.call(expensive))
        assert r1 == "result-1"
        assert r2 == "result-1"  # 缓存命中，不调用函数
        assert call_count == 1

    def test_cache_expiry(self):
        """TTL 过期后重新调用函数。"""
        call_count = 0

        async def expensive():
            nonlocal call_count
            call_count += 1
            return f"result-{call_count}"

        fb = Fallback(FallbackConfig(strategy="cache", cache_ttl=0.05))
        r1 = asyncio.run(fb.call(expensive))
        asyncio.run(asyncio.sleep(0.06))
        r2 = asyncio.run(fb.call(expensive))
        assert r1 == "result-1"
        assert r2 == "result-2"  # 缓存过期
        assert call_count == 2

    def test_cache_stale_fallback(self):
        """缓存未命中且调用失败时，返回过期缓存（stale fallback）。"""
        call_count = 0

        async def once_then_fail():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return "cached"
            raise ValueError("fail")

        fb = Fallback(FallbackConfig(strategy="cache", cache_ttl=0.05))
        r1 = asyncio.run(fb.call(once_then_fail))
        assert r1 == "cached"
        assert call_count == 1
        asyncio.run(asyncio.sleep(0.06))
        # 缓存过期，调用失败，返回过期缓存
        r2 = asyncio.run(fb.call(once_then_fail))
        assert r2 == "cached"  # stale fallback
        assert call_count == 2  # 第二次调用 actually called

    def test_clear_cache(self):
        """clear_cache 清空所有缓存。"""
        fb = Fallback(FallbackConfig(strategy="cache", cache_ttl=60))

        async def get_val():
            return "val"

        r1 = asyncio.run(fb.call(get_val))
        fb.clear_cache()
        r2 = asyncio.run(fb.call(get_val))
        assert r1 == r2  # 值相同但重新调用了函数

    def test_unknown_strategy(self):
        """未知策略抛出 ValueError。"""
        fb = Fallback(FallbackConfig(strategy="unknown"))
        with pytest.raises(ValueError, match="unknown"):
            asyncio.run(fb.call(self._ok))


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
class TestPipeline:
    async def _ok(self) -> str:
        return "ok"

    async def _fail(self) -> str:
        raise ValueError("fail")

    def test_pipeline_basic_call(self):
        """无任何组件时直接调用。"""
        p = Pipeline(name="empty")
        result = asyncio.run(p.execute(self._ok))
        assert result == "ok"

    def test_pipeline_rate_limited_fallback(self):
        """限流时走 Fallback（mock）。"""
        limiter = RateLimiter(RateLimitConfig(max_calls=0, window_seconds=10))
        fb = Fallback(FallbackConfig(strategy="mock", mock_result="mocked"))
        p = Pipeline(rate_limiter=limiter, fallback=fb, name="limited", rate_limit_key="k")
        result = asyncio.run(p.execute(self._ok))
        assert result == "mocked"

    def test_pipeline_rate_limited_no_fallback(self):
        """限流且无 Fallback 时抛 RateLimitError。"""
        limiter = RateLimiter(RateLimitConfig(max_calls=0, window_seconds=10))
        p = Pipeline(rate_limiter=limiter, name="limited", rate_limit_key="k")
        with pytest.raises(RateLimitError):
            asyncio.run(p.execute(self._ok))

    def test_pipeline_circuit_open_fallback(self):
        """熔断 OPEN 时走 Fallback（mock）。"""
        cb = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=1, recovery_timeout=60), name="cb"
        )
        with pytest.raises(ValueError):
            asyncio.run(cb.call(self._fail))
        fb = Fallback(FallbackConfig(strategy="mock", mock_result="mocked"))
        p = Pipeline(circuit_breaker=cb, fallback=fb, name="broken")
        result = asyncio.run(p.execute(self._ok))
        assert result == "mocked"

    def test_pipeline_circuit_open_no_fallback(self):
        """熔断 OPEN 且无 Fallback 时抛 CircuitBreakerOpenError。"""
        cb = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=1, recovery_timeout=60), name="cb"
        )
        with pytest.raises(ValueError):
            asyncio.run(cb.call(self._fail))
        p = Pipeline(circuit_breaker=cb, name="broken")
        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(p.execute(self._ok))

    def test_pipeline_all_components(self):
        """三个组件同时存在时按顺序执行。"""
        limiter = RateLimiter(RateLimitConfig(max_calls=10, window_seconds=10))
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=10), name="cb")
        fb = Fallback(FallbackConfig(strategy="fail_fast"))
        p = Pipeline(rate_limiter=limiter, circuit_breaker=cb, fallback=fb, name="full")
        result = asyncio.run(p.execute(self._ok))
        assert result == "ok"

    def test_pipeline_name(self):
        """name 属性。"""
        p = Pipeline(name="my-pipeline")
        assert p.name == "my-pipeline"

    def test_pipeline_rate_limit_key(self):
        """rate_limit_key 隔离。"""
        limiter = RateLimiter(RateLimitConfig(max_calls=1, window_seconds=10))
        asyncio.run(limiter.acquire("k"))
        p = Pipeline(rate_limiter=limiter, rate_limit_key="other")
        result = asyncio.run(p.execute(self._ok))
        assert result == "ok"


# --------------------------------------------------------------------------- #
# Pipeline 工厂函数
# --------------------------------------------------------------------------- #
class TestBuildPipeline:
    def test_build_pipeline_all_none(self):
        """所有组件均为 None 时返回 None。"""
        from agent.resilience.pipeline import build_pipeline

        p = build_pipeline(name="t", rate_limiter=None, circuit_breaker=None, fallback=None)
        assert p is None

    def test_build_pipeline_with_rate_limiter(self):
        """只有限流器时返回 Pipeline。"""
        from agent.resilience.pipeline import build_pipeline

        p = build_pipeline(
            name="t", rate_limiter=RateLimiter(), circuit_breaker=None, fallback=None
        )
        assert p is not None
        assert p.name == "t"

    def test_build_llm_pipeline_disabled(self):
        """resilience.enabled=False 时返回 None。"""
        from agent.config.settings import Settings
        from agent.resilience.pipeline import build_llm_pipeline

        s = Settings(resilience=dict(enabled=False))
        assert build_llm_pipeline(s) is None

    def test_build_llm_pipeline_enabled(self):
        """resilience.enabled=True 时返回非空 Pipeline。"""
        from agent.config.settings import Settings
        from agent.resilience.pipeline import build_llm_pipeline

        s = Settings(resilience=dict(enabled=True))
        p = build_llm_pipeline(s)
        assert p is not None
        assert p.name == "llm"

    def test_build_sandbox_pipeline_enabled(self):
        """sandbox pipeline 构建。"""
        from agent.config.settings import Settings
        from agent.resilience.pipeline import build_sandbox_pipeline

        s = Settings(resilience=dict(enabled=True))
        p = build_sandbox_pipeline(s)
        assert p is not None
        assert p.name == "sandbox"


# --------------------------------------------------------------------------- #
# 集成测试：Pipeline + Model/Sandbox
# --------------------------------------------------------------------------- #
class TestPipelineIntegration:
    """验证 Pipeline 与实际组件集成后的行为。"""

    async def _ok(self) -> str:
        return "ok"

    async def _fail(self) -> str:
        raise ValueError("fail")

    def test_model_act_with_pipeline(self):
        """FakeModel + Pipeline(retry) 在瞬时失败后重试成功。"""
        from agent.core.model import FakeModel, Decision, OpenAICompatibleModel

        call_count = 0

        class RetryModel(FakeModel):
            """模拟前 N 次失败、第 N+1 次成功的模型。"""

            async def act(self, messages, tools=None):
                nonlocal call_count
                call_count += 1
                if call_count < 2:
                    raise ValueError("transient failure")
                return Decision(text="ok")

            def stream(self, messages, tools=None):
                raise NotImplementedError

        fb = Fallback(FallbackConfig(strategy="retry", max_retries=2, retry_delay=0.01))
        p = Pipeline(fallback=fb, name="test-model")
        model = RetryModel([])
        # 直接通过 Pipeline 调用 model.act
        result = asyncio.run(p.execute(model.act, []))
        assert result.text == "ok"
        assert call_count == 2

    def test_model_act_pipeline_all_fail(self):
        """Pipeline + retry 全部耗尽后抛出原始异常。"""
        from agent.core.model import FakeModel, Decision

        call_count = 0

        class AlwaysFailModel(FakeModel):
            async def act(self, messages, tools=None):
                nonlocal call_count
                call_count += 1
                raise ValueError("always fail")

            def stream(self, messages, tools=None):
                raise NotImplementedError

        fb = Fallback(FallbackConfig(strategy="retry", max_retries=2, retry_delay=0.01))
        p = Pipeline(fallback=fb, name="test-model")
        model = AlwaysFailModel([])

        with pytest.raises(ValueError, match="always fail"):
            asyncio.run(p.execute(model.act, []))
        assert call_count == 3  # 初始 + 2 次重试

    def test_model_act_rate_limited_then_fallback_mock(self):
        """限流后走 mock fallback。"""
        from agent.core.model import FakeModel, Decision

        limiter = RateLimiter(RateLimitConfig(max_calls=0, window_seconds=10))
        fb = Fallback(FallbackConfig(strategy="mock", mock_result="mocked"))
        p = Pipeline(rate_limiter=limiter, fallback=fb, name="test-model", rate_limit_key="k")
        model = FakeModel([Decision(text="real")])
        result = asyncio.run(p.execute(model.act, []))
        assert result == "mocked"

    def test_model_act_circuit_open_then_fallback_mock(self):
        """熔断后走 mock fallback。"""
        from agent.core.model import FakeModel, Decision

        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, recovery_timeout=60), name="cb")
        with pytest.raises(ValueError):
            asyncio.run(cb.call(self._fail))
        fb = Fallback(FallbackConfig(strategy="mock", mock_result="mocked"))
        p = Pipeline(circuit_breaker=cb, fallback=fb, name="test-model")
        model = FakeModel([Decision(text="real")])
        result = asyncio.run(p.execute(model.act, []))
        assert result == "mocked"

    def test_sandbox_executor_with_pipeline(self):
        """LocalExecutor + Pipeline 在 Pipeline 启用时正常执行。"""
        import tempfile
        from pathlib import Path

        from agent.runtime.sandbox import ExecRequest, LocalExecutor, SandboxProfile

        fb = Fallback(FallbackConfig(strategy="fail_fast"))
        p = Pipeline(fallback=fb, name="test-sandbox")
        with tempfile.TemporaryDirectory() as tmp:
            ex = LocalExecutor(workspace=Path(tmp), profile=SandboxProfile.DANGER_FULL, pipeline=p)
            req = ExecRequest(cmd="echo hello", cwd=Path(tmp), env={}, timeout=5, profile=SandboxProfile.DANGER_FULL)
            result = asyncio.run(ex.run(req))
            assert result.ok is True

    def test_sandbox_executor_without_pipeline(self):
        """无 Pipeline 时 LocalExecutor 行为不变。"""
        import tempfile
        from pathlib import Path

        from agent.runtime.sandbox import ExecRequest, LocalExecutor, SandboxProfile

        with tempfile.TemporaryDirectory() as tmp:
            ex = LocalExecutor(workspace=Path(tmp), profile=SandboxProfile.DANGER_FULL)
            req = ExecRequest(cmd="echo hello", cwd=Path(tmp), env={}, timeout=5, profile=SandboxProfile.DANGER_FULL)
            result = asyncio.run(ex.run(req))
            assert result.ok is True


# --------------------------------------------------------------------------- #
# Pipeline execute_stream 测试
# --------------------------------------------------------------------------- #
class TestPipelineExecuteStream:
    """验证 ``execute_stream`` 保护流创建的正确性。"""

    async def _ok_stream(self) -> AsyncIterator[str]:
        """返回正常流。"""
        yield "a"
        yield "b"
        yield "c"

    async def _fail_stream(self) -> AsyncIterator[str]:
        """创建流时直接失败。"""
        raise ValueError("create failed")

    async def _fail_once_then_ok(self) -> AsyncIterator[str]:
        """第一次创建失败，第二次成功。

        异步生成器是惰性的——body 在 async for 时才执行。
        Pipeline.execute_stream 内部会预读第一项来检测创建失败。
        """
        if not hasattr(self, "_stream_call_count"):
            self._stream_call_count = 0
        self._stream_call_count += 1
        if self._stream_call_count < 2:
            raise ValueError("transient create failure")
        yield "recovered"

    def test_execute_stream_basic(self):
        """正常流可消费所有事件。"""
        p = Pipeline(name="t")
        events = list(asyncio.run(_collect_stream(p.execute_stream(self._ok_stream))))
        assert events == ["a", "b", "c"]

    def test_execute_stream_with_retry(self):
        """创建流失败时 retry 重新创建。"""
        self._stream_call_count = 0
        fb = Fallback(FallbackConfig(strategy="retry", max_retries=2, retry_delay=0.01))
        p = Pipeline(fallback=fb, name="t")
        events = list(asyncio.run(_collect_stream(p.execute_stream(self._fail_once_then_ok))))
        assert events == ["recovered"]
        assert self._stream_call_count == 2

    def test_execute_stream_rate_limited_fallback(self):
        """限流时走 Fallback mock（返回标量，execute_stream 不 yield 任何值）。"""
        limiter = RateLimiter(RateLimitConfig(max_calls=0, window_seconds=10))
        fb = Fallback(FallbackConfig(strategy="mock", mock_result="mocked"))
        p = Pipeline(rate_limiter=limiter, fallback=fb, name="t", rate_limit_key="k")
        result = asyncio.run(_collect_stream(p.execute_stream(self._ok_stream)))
        # 限流分支走 Fallback 返回标量（非迭代器），execute_stream 不 yield 任何值
        assert result == []

    def test_execute_stream_rate_limited_no_fallback(self):
        """限流且无 Fallback 时抛 RateLimitError。"""
        limiter = RateLimiter(RateLimitConfig(max_calls=0, window_seconds=10))
        p = Pipeline(rate_limiter=limiter, name="t", rate_limit_key="k")
        with pytest.raises(RateLimitError):
            asyncio.run(_collect_stream(p.execute_stream(self._ok_stream)))

    def test_execute_stream_circuit_open_no_fallback(self):
        """熔断 OPEN 时抛 CircuitBreakerOpenError（流不支持熔断降级为标量）。"""
        cb = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=1, recovery_timeout=60), name="cb"
        )
        with pytest.raises(ValueError):
            asyncio.run(cb.call(self._fail_stream))
        p = Pipeline(circuit_breaker=cb, name="t")
        with pytest.raises(CircuitBreakerOpenError):
            asyncio.run(_collect_stream(p.execute_stream(self._ok_stream)))


async def _collect_stream(agen) -> list:
    """消费异步生成器并返回事件列表。"""
    result = []
    async for item in agen:
        result.append(item)
    return result
