"""压缩器协议（Compactor）。

压缩器只作用于上下文投影 ``conv``（可变历史），绝不触碰 ``EventStream`` 审计真相。
具体实现（Microcompact / Auto Compact / Session Memory Compact）在 M4.2–M4.4 落地。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent.core.model import Message


@runtime_checkable
class Compactor(Protocol):
    """压缩器协议：把 boundary 之前的历史折叠为更紧凑的表示。"""

    async def compact(
        self,
        conv: list[Message],
        boundary: int,
    ) -> list[Message]:
        """把 boundary 之前的历史压缩为摘要，返回替换后的消息列表。

        ``boundary`` 之前的消息是「已压缩区」，之后的消息是「活跃区」。
        实现必须保持 ``tool_use`` / ``tool_result`` 配对，禁止孤立任一方。
        """
        ...


__all__ = [
    "Compactor",
]
