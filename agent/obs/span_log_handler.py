"""SpanLogHandler：将标准 logging 调用自动归属到当前活跃 span。

取代手动的 ``span.log()`` 调用，降低接入门槛，消除两套 API 并行的心智负担。

用法：
    >>> import logging
    >>> from agent.obs.span_log_handler import SpanLogHandler, install_span_log_handler
    >>> install_span_log_handler()  # 向 root logger 注册 Handler
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


def install_span_log_handler(
    logger: logging.Logger | None = None,
    *,
    level: int = logging.DEBUG,
) -> SpanLogHandler:
    """向目标 logger 注册 SpanLogHandler（默认 root logger）。

    参数：
        logger: 目标 logger。为 None 时注册到 root logger（同步从根 logger 输出到 span）。
        level: Handler 的接收级别，默认 DEBUG 即全部日志都写入 span。

    返回：
        已注册的 SpanLogHandler 实例（可用于后续 removeHandler）。
    """
    target = logger or logging.getLogger()
    handler = SpanLogHandler(level=level)
    target.addHandler(handler)
    return handler
