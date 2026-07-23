"""Textual 传输实现（M8）：``AgentTransport`` 的全屏 TUI 实现，与 ``TerminalTransport`` 平级。

设计（见 `knowledge/调研-Textual全屏CLI重构方案.md` §4）：
- 仅实现协议，loop / session / core 零改动。
- ``on_event`` 是**公开方法**（对齐 ``TerminalTransport._on_event``），便于 daemon ``client`` 未来复用。
- 工作线程（M8.2 的 session driver）收到事件后，经 ``app.call_from_thread`` 把 UI 更新
  安全投递回 Textual 主线程。
- HITL（ask/approve/confirm_plan）在 M8.3 用 ModalScreen + 线程安全 Future 实现；
  本文件先放占位（raise NotImplementedError），不破坏非 HITL 路径。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any

from agent.core.events import Event, EventStream, EventType
from agent.core.intent import Question
from agent.core.transport import AgentTransport
from agent.runtime.approval import Action


class TextualTransport(AgentTransport):
    """``AgentTransport`` 的 Textual 实现：订阅 EventStream，把事件桥接为 UI 部件更新。"""

    def __init__(self, app: Any, *, context_mgr: Any | None = None) -> None:
        self._app = app
        self._context_mgr = context_mgr
        self._interactive = True
        self._tc_by_id: dict[str, Any] = {}
        self._stream: EventStream | None = None

    # ------------------------------------------------------------------ #
    # 协议属性
    # ------------------------------------------------------------------ #
    @property
    def interactive(self) -> bool:
        return self._interactive

    @property
    def context_mgr(self) -> Any | None:
        return self._context_mgr

    # ------------------------------------------------------------------ #
    # 事件订阅（渲染由 EventStream 驱动，取代 LoopPresenter 回调）
    # ------------------------------------------------------------------ #
    def bind(self, stream: EventStream) -> None:
        """订阅 EventStream（loop 每步新建流后调用）。重置工具调用收集表。"""
        self._tc_by_id = {}
        self._stream = stream
        stream.subscribe(self.on_event)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.unsubscribe(self.on_event)
            self._stream = None

    def on_event(self, ev: Event) -> None:
        """公开事件处理（对齐 TerminalTransport._on_event，供 daemon client 复用）。"""
        t = ev.type
        if t == EventType.TEXT:
            self._bridge(self._app.append_text, ev.text or "", ev.kind or "content")
        elif t == EventType.TOOL_USE:
            if ev.tool_use is not None:
                self._tc_by_id[ev.tool_use.id] = ev.tool_use
                self._bridge(self._app.append_tool_use, ev.tool_use)
        elif t == EventType.TOOL_CALL_DELTA:
            # 瞬时事件：M8.1 不单独渲染（M8.4 可接流式预览）；定稿由 TOOL_USE/TOOL_RESULT 展示。
            pass
        elif t == EventType.TOOL_RESULT:
            if ev.tool_call_id is not None:
                tc = self._tc_by_id.get(ev.tool_call_id)
                if tc is not None and ev.tool_result is not None:
                    self._bridge(self._app.update_tool_result, tc, ev.tool_result)
        elif t == EventType.PLAN_PROGRESS:
            self._bridge(self._app.append_plan_progress, ev)
        elif t == EventType.USER:
            self._bridge(self._app.append_user, ev.text or "")
        elif t == EventType.DECISION:
            # 一轮模型决策结束收尾（澄清/计划闸门提前返回时工具回调不触发，统一在此定稿）
            self._bridge(self._app.finalize_stream)
        # clarify / plan / final 由 HITL（show_questions/show_plan）或已流式文本覆盖，忽略

    def _bridge(self, target, *args) -> None:
        """把 UI 更新投递回 Textual 主线程（跨线程安全）。

        - 真实场景：`Session` 在独立 worker 线程跑，与 app 主线程不同 → 用
          ``app.call_from_thread``（Textual 官方跨线程 API）。
        - 测试场景（``run_test``）：测试协程与 app 同线程，``call_from_thread`` 会报错，
          直接调用即可（已处于 app 事件循环线程内，widget 变更安全）。
        """
        app = self._app
        app_thread = getattr(app, "_thread_id", None)
        if app_thread is None or threading.get_ident() == app_thread:
            target(*args)
        else:
            app.call_from_thread(target, *args)

    # ------------------------------------------------------------------ #
    # 非 HITL 渲染（HITL 由 app 方法统一呈现）
    # ------------------------------------------------------------------ #
    def show_questions(self, questions: list[Question]) -> None:
        for q in questions:
            extra = f"\n选项: {', '.join(q.options)}" if q.options else ""
            self._bridge(self._app.append_user, f"❓ {q.question}{extra}")

    def show_plan(self, res: Any) -> None:
        self._bridge(self._app.show_plan, res)

    def notify(self, message: str) -> None:
        self._bridge(self._app.notify, message)

    def show_skills(self, specs: list) -> None:
        self._bridge(self._app.show_skills, specs)

    def show_agents(self, specs: list) -> None:
        self._bridge(self._app.show_agents, specs)

    def report_usage(self, usage: dict[str, int] | None, answer: str | None = None) -> None:
        self._bridge(self._app.report_usage, usage, answer)

    # ------------------------------------------------------------------ #
    # HITL：在 worker 线程被 await；经 call_from_thread 把模态屏推到主线程，
    # 用户操作后由屏幕（主线程）set_result 唤醒。Future 跨线程安全；
    # worker 线程用 asyncio.wrap_future 挂回自己的事件循环。
    # ------------------------------------------------------------------ #
    async def ask(self, question: Question) -> str:
        from agent.tui.screens import AskScreen

        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._bridge(self._app.push_screen, AskScreen(question, fut))
        return await asyncio.wrap_future(fut)

    async def confirm_plan(self) -> bool:
        from agent.tui.screens import PlanScreen

        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._bridge(self._app.push_screen, PlanScreen(fut))
        return await asyncio.wrap_future(fut)

    async def approve(self, action: Action) -> bool:
        from agent.tui.screens import ApproveScreen

        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._bridge(self._app.push_screen, ApproveScreen(action, fut))
        return await asyncio.wrap_future(fut)
