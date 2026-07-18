"""可观测层：Trace / Span / Log（M3.1 增强）。

- ``Span.log()``：在 span 存活期内追加结构化日志（key-value + level + 时间戳）。
- ``TraceStore``：SQLite 持久化，支持 session 级别的 save/load/list。
- 渲染：``render()`` 树状展示父子 span + 日志摘要。

设计要点：
- 一个 span 可以记录多条 log，用于模型调用细节、工具参数/结果、错误详情。
- 持久化按 session_id 分区，覆盖写保证幂等。
- 与 ``Session`` 集成：每轮 step 结束自动保存。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# LogEntry：Span 内的一条结构化日志
# --------------------------------------------------------------------------- #
@dataclass
class LogEntry:
    """Span 内的一条结构化日志。"""

    ts: float
    key: str
    value: Any
    level: str = "info"  # info / warn / error


# --------------------------------------------------------------------------- #
# Span
# --------------------------------------------------------------------------- #
@dataclass
class Span:
    id: str
    name: str
    kind: str
    parent_id: str | None
    started_at: float
    ended_at: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    logs: list[LogEntry] = field(default_factory=list)

    def log(self, key: str, value: Any, level: str = "info") -> "Span":
        """在 span 存活期内追加一条结构化日志（含时间戳）。"""
        self.logs.append(LogEntry(ts=time.time(), key=key, value=value, level=level))
        return self


# --------------------------------------------------------------------------- #
# _SpanCtx：上下文管理器
# --------------------------------------------------------------------------- #
class _SpanCtx:
    def __init__(self, tracer: "Tracer", span: Span) -> None:
        self._tracer = tracer
        self.span = span

    def __enter__(self) -> Span:
        return self.span

    def __exit__(self, *exc: object) -> None:
        self.span.ended_at = time.time()

    def set(self, **meta: Any) -> "_SpanCtx":
        self.span.meta.update(meta)
        return self


# --------------------------------------------------------------------------- #
# Tracer
# --------------------------------------------------------------------------- #
class Tracer:
    """内存 trace 收集器。span 以父子树组织，可通过 ``render()`` 输出文本。"""

    def __init__(self, session_id: str | None = None) -> None:
        self.spans: list[Span] = []
        self.session_id: str = session_id or uuid.uuid4().hex[:12]

    def span(self, name: str, kind: str = "span", parent: Span | None = None) -> _SpanCtx:
        s = Span(
            id=uuid.uuid4().hex[:8],
            name=name,
            kind=kind,
            parent_id=parent.id if parent else None,
            started_at=time.time(),
        )
        self.spans.append(s)
        return _SpanCtx(self, s)

    def render(self) -> str:
        """树状文本渲染（含 box 连接符）；模型调用 span 仅展示 total token。"""
        lines: list[str] = []

        def walk(s: Span, prefix: str, is_last: bool) -> None:
            connector = "└─ " if is_last else "├─ "
            dur_ms = (s.ended_at or time.time()) - s.started_at
            lines.append(
                f"{prefix}{connector}{s.name} [{s.kind}] "
                f"{dur_ms * 1000:.1f}ms (id={s.id})"
            )
            # 日志摘要：最多展示 3 条最近的 warn/error 日志
            recent_logs = [lg for lg in s.logs if lg.level in ("warn", "error")][-3:]
            if recent_logs:
                child_prefix = prefix + ("   " if is_last else "│  ")
                for lg in recent_logs:
                    color = "yellow" if lg.level == "warn" else "red"
                    lines.append(
                        f"{child_prefix}└─ [{color}]{lg.key}[/{color}]: {lg.value}"
                    )
            # 模型调用 span：仅展示 total token（完整 usage 已存于 meta，供导出 Langfuse 等）
            if "usage" in s.meta:
                child_prefix = prefix + ("   " if is_last else "│  ")
                lines.append(f"{child_prefix}└─ total={s.meta['usage'].get('total_tokens', 0)} tok")
            children = [c for c in self.spans if c.parent_id == s.id]
            child_prefix = prefix + ("   " if is_last else "│  ")
            for i, child in enumerate(children):
                walk(child, child_prefix, i == len(children) - 1)

        roots = [s for s in self.spans if s.parent_id is None]
        for i, root in enumerate(roots):
            walk(root, "", i == len(roots) - 1)
        return "\n".join(lines)
