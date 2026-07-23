"""子 agent 的 Textual 传输（M8.6）：`_SubAgentTransport` 的 TUI 等价物。

设计（对齐 `agent/runtime/terminal_transport._SubAgentTransport`）：
- 子 agent 拥有**独立 EventStream**，但渲染不弹独立窗口，而是委派给父 ``ChatApp`` 的主消息区
  （``#log``），每条子 agent 消息以「▶ subagent: <name>」前缀汇入，层级清晰。
- 屏蔽独立 HITL：澄清/审批经 ``self._parent`` 委派给父传输统一决策（与现状一致：子 agent 不
  自作主张弹审批框）。
- 复用 ``TextualTransport`` 的事件桥接（``_bridge`` 经 ``app.call_from_thread`` 回主线程）。

不变量：子 agent 独立 EventStream 不变；旧 ``_SubAgentTransport`` 保留供旧 TerminalTransport CLI。
"""

from __future__ import annotations

from typing import Any

from agent.core.events import EventType
from agent.runtime.textual_transport import TextualTransport


class _SubAgentTuiTransport(TextualTransport):
    """子 agent 的 Textual 传输：渲染委派父 ``ChatApp``，前缀「▶ subagent: <name>」。

    与 ``TextualTransport`` 的区别：
      - 文本事件以子 agent 前缀汇入主消息区（而非裸文本）；
      - HITL（ask / approve / confirm_plan）委派给父传输，避免子 agent 弹独立模态；
      - 非 HITL 事件（工具块 / 计划进度 / 用户）保持与父 App 同样的渲染部件。
    """

    def __init__(
        self,
        parent: TextualTransport | None = None,
        *,
        name: str = "subagent",
        context_mgr: Any | None = None,
    ) -> None:
        # 复用父传输绑定的 App（父与主 UI 同一实例）
        app = parent._app if parent is not None else None
        super().__init__(app, context_mgr=context_mgr)
        self._parent = parent
        self._name = name
        self._prefixed = False  # 当前连续文本流是否已加过子 agent 前缀

    # ------------------------------------------------------------------ #
    # HITL 委派给父传输（屏蔽子 agent 独立弹窗）
    # ------------------------------------------------------------------ #
    async def ask(self, question: Any) -> str:
        if self._parent is not None:
            return await self._parent.ask(question)
        return await super().ask(question)

    async def approve(self, action: Any) -> bool:
        if self._parent is not None:
            return await self._parent.approve(action)
        return await super().approve(action)

    async def confirm_plan(self) -> bool:
        if self._parent is not None:
            return await self._parent.confirm_plan()
        return await super().confirm_plan()

    # ------------------------------------------------------------------ #
    # 事件 → 主区渲染（前缀汇入）
    # ------------------------------------------------------------------ #
    def on_event(self, ev: Any) -> None:
        t = ev.type
        if t == EventType.TEXT:
            # 连续文本流首块加前缀；后续块直接续写同一消息
            text = ev.text or ""
            if not self._prefixed:
                self._bridge(
                    self._app.append_text,
                    f"▶ subagent: {self._name}\n{text}",
                    ev.kind or "content",
                )
                self._prefixed = True
            else:
                self._bridge(self._app.append_text, text, ev.kind or "content")
            return

        # 非文本事件：重置前缀标记（下一次文本是新消息，需重新加前缀）；其余委派父 App 渲染
        if t in (
            EventType.TOOL_USE,
            EventType.DECISION,
            EventType.USER,
            EventType.PLAN_PROGRESS,
        ):
            self._prefixed = False
        super().on_event(ev)
