"""SpanLogHandler：将标准 logging 调用自动归属到当前活跃 span。

取代手动的 ``span.log()`` 调用，降低接入门槛，消除两套 API 并行的心智负担。

用法：
    >>> import logging
    >>> from agent.obs.span_log_handler import ensure_span_log_handler
    >>> ensure_span_log_handler()  # 向 root logger 注册 Handler（幂等）
    >>> logger = logging.getLogger(__name__)
    >>> logger.info("tool %s completed", tool_name)  # 自动写入当前 span
"""

from __future__ import annotations

import logging
from typing import Any

from agent.obs.tracer import _CURRENT_SPAN

# Python logging level → span.log level 映射
_LOG_LEVEL_MAP: dict[int, str] = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}

# 模块级状态：防止重复注册
_installed: bool = False


class SpanLogHandler(logging.Handler):
    """logging Handler：感知当前活跃 span 并自动写入结构化日志。

    通过 ``_CURRENT_SPAN`` ContextVar 获取当前 span（与 ``_SpanCtx`` 使用相同上下文）。
    无活跃 span 时静默 no-op，不干扰普通 logging 行为。
    """

    def __init__(self, level: int = logging.NOTSET) -> None:
        super().__init__(level)

    def emit(self, record: logging.LogRecord) -> None:
        span = _CURRENT_SPAN.get(None)
        if span is None:
            return
        # 将 key 设为 logger name，方便按模块过滤
        span.log(
            key=record.name,
            value={
                "msg": record.getMessage(),
                "module": record.module,
                "line": record.lineno,
                "func": record.funcName,
                **(record.__dict__.get("extra") or {}),
            },
            level=_LOG_LEVEL_MAP.get(record.levelno, "info"),
        )


def ensure_span_log_handler(
    logger: logging.Logger | None = None,
    *,
    level: int = logging.DEBUG,
) -> SpanLogHandler | None:
    """向目标 logger 注册 SpanLogHandler（幂等，防止重复注册）。

    参数：
        logger: 目标 logger。为 None 时注册到 root logger。
        level: Handler 的接收级别，默认 DEBUG。

    返回：
        首次注册返回 SpanLogHandler 实例；已注册过则返回 None。
    """
    global _installed
    if _installed:
        return None
    target = logger or logging.getLogger()
    handler = SpanLogHandler(level=level)
    target.addHandler(handler)
    _installed = True
    return handler
