"""上下文与记忆管理包（M4 上下文与记忆里程碑）。

对外导出核心类型，便于 ``from agent.context import ContextManager, ContextUsage``。
"""

from __future__ import annotations

from agent.context.compactors import Compactor
from agent.context.compactors.auto_compact import AutoCompact
from agent.context.compactors.microcompact import (
    COMPACTABLE_TOOLS,
    PLACEHOLDER,
    Microcompact,
)
from agent.context.compactors.session_memory import (
    MEMORY_SYSTEM_PROMPT,
    SUMMARY_SECTIONS,
    SessionMemory,
    SessionMemoryConfig,
)
from agent.context.manager import CompactRecord, ContextManager, ContextUsage

__all__ = [
    "ContextManager",
    "ContextUsage",
    "CompactRecord",
    "Compactor",
    "Microcompact",
    "AutoCompact",
    "SessionMemory",
    "SessionMemoryConfig",
    "SUMMARY_SECTIONS",
    "MEMORY_SYSTEM_PROMPT",
    "COMPACTABLE_TOOLS",
    "PLACEHOLDER",
]
