"""可观测层：占位 Tracer（M5 再接 OTel / Langfuse）。

当前实现把 span 记录在内存，支持父子关系（parent_id）与树状渲染。
每条 span：id / name / kind / parent_id / 起止时间 / meta。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Span:
    id: str
    name: str
    kind: str
    parent_id: str | None
    started_at: float
    ended_at: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


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


class Tracer:
    def __init__(self) -> None:
        self.spans: list[Span] = []

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
