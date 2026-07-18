"""韧性层核心组件（M3.2）。

提供三大确定性组件：``RateLimiter``、``CircuitBreaker``、``Fallback``，
以及可组合的 ``Pipeline`` 包装器（M3.3 完善）。

设计原则：
- 所有组件是纯异步、无阻塞的（asyncio 原语）。
- 配置由 ``Settings.resilience`` 注入，组件构造后不可变。
- 组件间可组合（Pipeline），也可独立使用。
"""

from agent.resilience.rate_limiter import RateLimiter, RateLimitConfig, RateLimitError
from agent.resilience.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitBreakerOpenError
from agent.resilience.fallback import Fallback, FallbackConfig
from agent.resilience.health import CheckResult, HealthChecker, HealthStatus, HealthHTTPHandler, build_default_health_checks
from agent.resilience.pipeline import Pipeline, build_llm_pipeline, build_pipeline, build_sandbox_pipeline

__all__ = [
    "RateLimiter", "RateLimitConfig", "RateLimitError",
    "CircuitBreaker", "CircuitBreakerConfig", "CircuitBreakerOpenError",
    "Fallback", "FallbackConfig",
    "Pipeline",
    "build_pipeline", "build_llm_pipeline", "build_sandbox_pipeline",
    "HealthChecker", "HealthStatus", "CheckResult", "build_default_health_checks",
]
