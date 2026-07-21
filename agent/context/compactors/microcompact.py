"""Microcompact 压缩器（M4.2，零成本）。

在每次 API 请求前，把 ``conv`` 中 **boundary 之前** 较旧的 ``role="tool"`` 消息的
``content`` 替换为占位符 ``[Old tool result content cleared]``，保留最近 ``keep_recent``
个工具结果不动。

设计要点（见 milestones/M4.../4.2）：
- 零 API 调用，纯字符串替换；消息条数不变，索引不变。
- 只改 ``content``，保留 ``tool_call_id``，不拆散 ``tool_use`` / ``tool_result`` 配对。
- 作用于**整个当前 conv**（``boundary = len(conv)``），而非仅 ``compact_boundary`` 之前：
  边界之前的内容已被 auto-compact 压成摘要（无 ``role="tool"`` 长结果可清），且会话前期
  ``compact_boundary=0`` 会使 Microcompact 整体失效；故独立于 auto-compact 边界处理全量。
- ``keep_recent`` 默认 5：保留最近的工具结果，否则模型看不到当前正在改的代码。
- 只压缩来自「大输出类工具」（bash/read/grep/glob/write/edit/find）的结果；通过
  ``tool_call_id`` 交叉 assistant 消息的 ``tool_calls`` 得到工具名，确保非大输出工具
  （如 AskUserQuestion 的回执）不被误压缩。
"""

from __future__ import annotations

from agent.core.model import Message

# 可压缩的工具结果类型（大输出类工具）。非这些工具的结果（如审批/问答回执）保留原样。
COMPACTABLE_TOOLS = ("bash", "read", "grep", "glob", "write", "edit", "find")

# 占位符字符串，对齐 Claude Code 行为。
PLACEHOLDER = "[Old tool result content cleared]"


class Microcompact:
    """Microcompact 压缩器：零成本，仅替换旧 tool_result 内容为占位符。"""

    def __init__(
        self,
        keep_recent: int = 5,
        compactable_tools: list[str] | None = None,
    ) -> None:
        self.keep_recent = keep_recent
        self.compactable_tools = tuple(compactable_tools or list(COMPACTABLE_TOOLS))

    async def compact(self, conv: list[Message], boundary: int) -> list[Message]:
        """对 conv 执行 Microcompact，返回同一列表（原地替换 content）。

        ``boundary`` 之前的消息是「已压缩区」，之后的消息是「活跃区」。
        实现保持 ``tool_use`` / ``tool_result`` 配对，禁止孤立任一方。
        """
        if boundary <= 0 or boundary > len(conv):
            return conv

        # 构建 tool_call_id → 工具名 映射（从 assistant 消息的 tool_calls 交叉得到）
        id_to_name = self._build_id_to_name(conv)

        # 收集 boundary 之前所有「可压缩的 tool 消息」索引
        tool_indices: list[int] = [
            i
            for i, msg in enumerate(conv[:boundary])
            if msg.role == "tool" and self._is_compactable(msg, id_to_name)
        ]

        # 只替换「较早的」部分，保留最近 keep_recent 个不动
        if len(tool_indices) > self.keep_recent:
            replace = set(tool_indices[: len(tool_indices) - self.keep_recent])
        else:
            replace = set()

        for i in replace:
            conv[i] = Message(
                role="tool",
                content=PLACEHOLDER,
                tool_call_id=conv[i].tool_call_id,
            )
        return conv

    def _build_id_to_name(self, conv: list[Message]) -> dict[str, str]:
        """从 assistant 消息的 tool_calls 构建 tool_call_id → 工具名 映射。"""
        mapping: dict[str, str] = {}
        for msg in conv:
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    mapping[tc.id] = tc.name
        return mapping

    def _is_compactable(self, msg: Message, id_to_name: dict[str, str]) -> bool:
        """判断一条 tool 消息是否应被占位替换。

        - content 必须存在且长度 > 100（短结果无压缩价值）。
        - 若 ``compactable_tools`` 非空，仅压缩来自这些大输出类工具的结果
          （通过 tool_call_id 交叉验证工具名）；否则任意长 tool 结果都可压缩。
        """
        if not (msg.content and len(msg.content) > 100):
            return False
        if not self.compactable_tools:
            return True
        name = id_to_name.get(msg.tool_call_id or "")
        return name in self.compactable_tools
