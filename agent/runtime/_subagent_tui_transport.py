"""子 agent 的 Textual 传输（M8.6/M8.8）：`_SubAgentTransport` 的 TUI 等价物。

设计（对齐 `agent/runtime/terminal_transport._SubAgentTransport`）：
- 子 agent 拥有**独立 EventStream**，但渲染不弹独立窗口，而是汇入父 ``ChatApp`` 的**专属子 agent 块**
  （``SubagentBlock``，见 `agent/tui/render.py`）；块带标题「🤖 subagent: <name>」，可整体折叠，
  块内消息流/工具块渲染与主区共用 ``MessageRenderer`` 逻辑。
- 屏蔽独立 HITL：澄清/审批经 ``self._parent`` 委派给父传输统一决策（与现状一致：子 agent 不
  自作主张弹审批框）。
- 复用 ``TextualTransport`` 的事件桥接（``_bridge`` 经 ``app.call_from_thread`` 回主线程）。

不变量：子 agent 独立 EventStream 不变；旧 ``_SubAgentTransport`` 保留供旧 TerminalTransport CLI。
"""

from __future__ import annotations

import concurrent.futures
import threading
from typing import Any

from agent.core.events import EventType
from agent.runtime.textual_transport import TextualTransport


class _SubAgentTuiTransport(TextualTransport):
    """子 agent 的 Textual 传输：渲染汇入父 App 的专属 ``SubagentBlock``。

    与 ``TextualTransport`` 的区别：
      - 文本/工具/计划/用户事件路由到子 agent 块（而非主区裸文本）；
      - HITL（ask / approve / confirm_plan）委派给父传输，避免子 agent 弹独立模态；
      - 块不存在时（极短竞态）退回主区渲染，待块就绪后自然分流。
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

    # ------------------------------------------------------------------ #
    # 取得/创建子 agent 块（跨线程安全：worker 线程阻塞等块创建完成）
    # ------------------------------------------------------------------ #
    def _get_block(self) -> Any:
        app = self._app
        blocks = getattr(app, "_subagent_blocks", None)
        if blocks is None:
            return None
        if self._name in blocks:
            return blocks[self._name]
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def _create() -> None:
            blk = app.ensure_subagent_block(self._name)
            fut.set_result(blk)

        if getattr(app, "_thread_id", None) is None or threading.get_ident() == getattr(
            app, "_thread_id", None
        ):
            _create()
        else:
            app.call_from_thread(_create)
        return fut.result()

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
    # 事件 → 子 agent 块渲染（HITL 委派父传输）
    # ------------------------------------------------------------------ #
    def on_event(self, ev: Any) -> None:
        t = ev.type
        if t == EventType.TEXT:
            block = self._get_block()
            if block is not None:
                self._bridge(block.append_text, ev.text or "", ev.kind or "content")
                return
            super().on_event(ev)
            return
        if t == EventType.TOOL_USE:
            block = self._get_block()
            if block is not None and ev.tool_use is not None:
                self._tc_by_id[ev.tool_use.id] = ev.tool_use
                self._bridge(block.append_tool_use, ev.tool_use)
                return
            super().on_event(ev)
            return
        if t == EventType.TOOL_RESULT:
            block = self._get_block()
            if block is not None and ev.tool_call_id is not None:
                tc = self._tc_by_id.get(ev.tool_call_id)
                if tc is not None and ev.tool_result is not None:
                    self._bridge(block.update_tool_result, tc, ev.tool_result)
                    return
            super().on_event(ev)
            return
        if t == EventType.PLAN_PROGRESS:
            block = self._get_block()
            if block is not None:
                self._bridge(block.append_plan_progress, ev)
                return
            super().on_event(ev)
            return
        if t == EventType.USER:
            block = self._get_block()
            if block is not None:
                self._bridge(block.append_user, ev.text or "")
                return
            super().on_event(ev)
            return
        if t == EventType.DECISION:
            block = self._get_block()
            if block is not None:
                self._bridge(block.finalize_stream)
                return
            super().on_event(ev)
            return
        # 其余（HITL 等）走默认（HITL 经父传输委派）
        super().on_event(ev)
