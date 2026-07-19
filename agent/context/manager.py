"""上下文管理器（ContextManager）基础。

持有 ``conv`` 投影、估算 token 占用分类明细、记录压缩历史与 Compact Boundary 位置。
本模块**只做计量与边界管理**，不执行任何压缩（压缩委托给 ``Compactor``，M4.2+）。

设计要点（见 milestones/M4.../4.1）：
- 有效窗口 ``effective_window = context_window − min(max_output_tokens, 20000)``：
  保留最大输出预算，避免输出被截断。
- 压缩阈值 ``compact_threshold = effective_window − compact_buffer``（默认 13K）；
  ``should_compact`` 在 ``used_pct >= 93%`` 时触发（≈ effective_window − compact_buffer）。
- Compact Boundary：``compact_boundary`` 索引之前的内容已被压缩，计量时只统计其后。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from agent.context.compactors.microcompact import Microcompact
from agent.context.tokens import _estimate_tokens
from agent.core.model import Message


@dataclass
class ContextUsage:
    """上下文占用分类明细。"""

    system_fixed: int = 0     # System Prompt 静态段（可缓存）
    system_dynamic: int = 0   # System Prompt 动态段（日期/Git 状态等）
    tools: int = 0            # Tools 列表
    messages: int = 0         # 对话历史（user/assistant/tool_result）
    total: int = 0            # 总和
    available: int = 0        # 有效窗口 − total（剩余可用）
    used_pct: float = 0.0     # total / 有效窗口


@dataclass
class CompactRecord:
    """一次压缩的记录。"""

    ts: float                  # 时间戳
    method: str                # "microcompact" / "auto_compact" / "session_memory"
    before_tokens: int
    after_tokens: int


# 压缩触发的占用比例阈值（≈ effective_window − compact_buffer）
_COMPACT_TRIGGER_PCT = 0.93


class ContextManager:
    def __init__(
        self,
        context_window: int = 200_000,
        max_output_tokens: int = 20_000,
        compact_buffer: int = 13_000,
        *,
        system_fixed_tokens: int = 3_000,
        system_dynamic_tokens: int = 0,
        tools_tokens: int = 15_000,
        microcompact_keep_recent: int = 5,
        microcompact: Microcompact | None = None,
    ):
        self.conv: list[Message] = []
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.compact_buffer = compact_buffer
        self.effective_window = context_window - min(max_output_tokens, 20_000)
        self.compact_threshold = self.effective_window - compact_buffer
        self.compact_boundary: int = 0  # 索引边界：之前的内容已被压缩
        self.history: list[CompactRecord] = []
        self._system_fixed = system_fixed_tokens
        self._system_dynamic = system_dynamic_tokens
        self._tools = tools_tokens
        self.microcompact = microcompact or Microcompact(keep_recent=microcompact_keep_recent)

    def estimate_usage(self) -> ContextUsage:
        """估算当前上下文占用分类明细。"""
        msg_tokens = self._estimate_conv_tokens()
        fixed = self._system_fixed
        dynamic = self._system_dynamic
        tools = self._tools
        total = fixed + dynamic + tools + msg_tokens
        available = max(0, self.effective_window - total)
        used_pct = total / self.effective_window if self.effective_window > 0 else 0.0
        return ContextUsage(fixed, dynamic, tools, msg_tokens, total, available, used_pct)

    def should_compact(self) -> bool:
        """是否需要触发压缩？"""
        usage = self.estimate_usage()
        return usage.used_pct >= _COMPACT_TRIGGER_PCT

    def mark_boundary(self) -> None:
        """记录当前 conv 索引为压缩边界。"""
        self.compact_boundary = len(self.conv)

    async def apply_microcompact(self) -> list[Message]:
        """执行 Microcompact（零成本），返回处理后的 conv（原地替换旧 tool 内容）。

        Microcompact 作用于**整个当前 conv**（boundary = len(conv)），每次 API 请求前
        清除较旧的大输出类工具结果，保留最近 ``keep_recent`` 个。它独立于 auto-compact
        的 ``compact_boundary``：auto-compacted 区域通常是摘要而非 tool 结果，不会被误伤。
        空 conv 直接原样返回。
        """
        if not self.conv:
            return self.conv
        return await self.microcompact.compact(self.conv, len(self.conv))

    def record_compact(
        self,
        method: str,
        before_tokens: int,
        after_tokens: int,
    ) -> CompactRecord:
        """追加一条压缩记录到历史。"""
        rec = CompactRecord(
            ts=time.time(),
            method=method,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
        )
        self.history.append(rec)
        return rec

    def _estimate_conv_tokens(self) -> int:
        """估算 conv 中 boundary 之后的 token 数。"""
        text = ""
        for msg in self.conv[self.compact_boundary:]:
            text += (msg.content or "")
            if msg.tool_call_id:
                text += msg.tool_call_id
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    text += tc.name + json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False)
        return _estimate_tokens(text)

    def set_conv(self, conv: list[Message]) -> None:
        """设置当前上下文投影（每次 loop.run 前调用）。"""
        self.conv = conv

    def get_active_messages(self) -> list[Message]:
        """获取 boundary 之后的有效消息（压缩前原文 + 压缩后消息）。"""
        return list(self.conv)
