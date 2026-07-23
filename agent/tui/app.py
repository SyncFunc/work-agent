"""Textual 全屏 chat 应用（M8）。

架构铁律（见 `knowledge/调研-Textual全屏CLI重构方案.md`）：
- 本类只做「渲染事件 + 收集输入 + HITL 模态」，**不含任何 loop / 工具逻辑**。
- 真正推理由 ``Session`` 在独立 worker 线程跑 ``Session.step`` 驱动（M8.2），
  事件经 ``TextualTransport`` 用 ``app.call_from_thread`` 安全桥接回主线程。
- ``AgentTransport`` 协议 / ``EventStream`` / loop / daemon 协议**一字不改**。
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from typing import TYPE_CHECKING, Any

from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Static, TextArea

from agent.core.session_command import dispatch_command
from agent.runtime.textual_transport import TextualTransport
from agent.tui.widgets import (
    AssistantMessage,
    ReasoningMessage,
    ToolBlock,
    UserMessage,
)

if TYPE_CHECKING:
    from agent.config.settings import Settings
    from agent.core.session import Session


_SENTINEL = object()  # 任务队列哨兵：通知 worker 线程退出


class _StaticLine(Static):
    """把任意 Rich renderable 包成一个可挂载的静态行。"""

    def __init__(self, renderable: Any) -> None:
        super().__init__(renderable)


class ChatApp(App):
    """全屏 chat 应用：顶部状态栏 + 滚动消息区 + 底部输入 + 快捷键栏。"""

    CSS = """
    #log { height: 1fr; }
    #input { height: 5; }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        ("ctrl+c", "quit", "退出"),
        ("ctrl+j", "submit", "发送"),
    ]

    def __init__(
        self,
        session: Session | None = None,
        settings: Settings | None = None,
        session_store: Any | None = None,
        transport: TextualTransport | None = None,
    ) -> None:
        super().__init__()
        self.session = session
        self.settings = settings
        self.session_store = session_store
        self.transport = transport
        # 流式渲染状态（M8.1/M8.4）
        self._current: Any = None  # 当前正在流式更新的消息部件
        self._current_is_reasoning = False
        self._tool_blocks: dict[str, Any] = {}  # tool_call_id -> ToolBlock
        self._plan_steps: list[Any] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="log")
        yield TextArea(id="input", theme="css", tab_behavior="indent")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Agent · 全屏会话"
        self._mount(
            UserMessage("欢迎使用全屏 chat（Ctrl+Q 退出；Ctrl+J 发送；输入 / 查看命令）。")
        )
        # 有 session 时：创建 transport 并启动 worker 线程驱动 Session.step（方案 B）。
        if self.session is not None:
            self.transport = TextualTransport(self, context_mgr=self.session.context_mgr)
            self._start_driver()
        self.query_one(TextArea).focus()

    def on_unmount(self) -> None:
        self._stop_driver()

    # ------------------------------------------------------------------ #
    # 输入与 chat 循环（方案 B：thread worker 跑 Session.step）
    # ------------------------------------------------------------------ #
    async def action_submit(self) -> None:
        """Ctrl+J：提交输入。空输入忽略；exit/quit 退出；/ 命令走 dispatch_command；
        其余作为任务投递给 worker 线程跑 Session.step。"""
        ta = self.query_one(TextArea)
        text = ta.text.strip()
        if not text:
            return
        ta.text = ""
        low = text.lower()
        if low in {"exit", "quit"}:
            self.exit()
            return
        if text.startswith("/"):
            await self._handle_command(text)
            return
        if self.session is None or not hasattr(self, "_task_queue"):
            self.notify("（无活动会话，无法提交任务）")
            return
        self._task_queue.put(text)

    async def _handle_command(self, raw: str) -> None:
        if self.session is None or self.transport is None:
            self.notify("（无活动会话）")
            return
        handled = await dispatch_command(
            self.session, raw, self.transport, self.settings, feedback=self.notify
        )
        if not handled:
            self.notify(f"未知命令: {raw}")

    def _start_driver(self) -> None:
        """启动独立线程 + 独立 asyncio loop 消费任务队列，跑 Session.step。"""
        self._task_queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()

    def _stop_driver(self) -> None:
        if hasattr(self, "_worker") and self._worker is not None:
            self._task_queue.put(_SENTINEL)
            self._worker.join(timeout=2)

    def _run_worker(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._worker_loop = loop
        try:
            loop.run_until_complete(self._session_loop())
        finally:
            loop.close()

    async def _session_loop(self) -> None:
        """消费任务队列，逐轮跑 Session.step；事件经 TextualTransport 桥接回主线程。"""
        while True:
            task = await asyncio.get_running_loop().run_in_executor(None, self._task_queue.get)
            if task is _SENTINEL:
                break
            try:
                res, err = await self.session.step(
                    task, self.transport, yes=False, fatal_plan_decline=False
                )
            except Exception as e:  # 任何未捕获异常优雅通知，不崩 UI
                self.transport.notify(f"error: {type(e).__name__}: {e}")
                res, err = None, 1
            else:
                if res is not None:
                    self.transport.report_usage(res.usage, res.text)
            # 一轮结束：收尾 transport（退订当前流、刷通知）
            self.transport.close()

    # ------------------------------------------------------------------ #
    # 内部工具：挂载部件 + 自动吸底
    # ------------------------------------------------------------------ #
    def _mount(self, widget: Any) -> None:
        log = self.query_one("#log", VerticalScroll)
        log.mount(widget)
        log.scroll_end(animate=False)

    # ------------------------------------------------------------------ #
    # 事件 → 部件 渲染（由 TextualTransport 经 call_from_thread 调用）
    # ------------------------------------------------------------------ #
    def append_user(self, text: str) -> None:
        self._mount(UserMessage(text))

    def append_text(self, text: str, kind: str) -> None:
        if kind == "reasoning":
            if self._current is None or not self._current_is_reasoning:
                self._current = ReasoningMessage()
                self._current_is_reasoning = True
                self._mount(self._current)
            self._current.append(text)
        else:
            if self._current is None or self._current_is_reasoning:
                self._current = AssistantMessage()
                self._current_is_reasoning = False
                self._mount(self._current)
            self._current.append(text)

    def append_tool_use(self, tc: Any) -> None:
        # 工具调用前先定稿当前流式消息
        self._current = None
        self._current_is_reasoning = False
        args = json.dumps(tc.arguments, ensure_ascii=False, indent=2)
        block = ToolBlock(tc.name, args)
        self._tool_blocks[tc.id] = block
        self._mount(block)

    def update_tool_result(self, tc: Any, res: Any) -> None:
        block = self._tool_blocks.get(tc.id)
        if block is not None:
            block.set_result(res.output or res.error or "", res.ok)

    def append_plan_progress(self, ev: Any) -> None:
        upd = ev.plan_update or {}
        line = f"📋 计划进度: {upd.get('step_id', '?')} → {upd.get('status', '?')}"
        if upd.get("note"):
            line += f"  ({upd['note']})"
        self._mount(_StaticLine(line))

    def finalize_stream(self) -> None:
        # 一轮决策结束：定稿当前流式块（下次文本另起新块）
        self._current = None
        self._current_is_reasoning = False

    def show_plan(self, res: Any) -> None:
        parts: list[Any] = []
        if res.plan:
            parts.append(Markdown(res.plan))
        if res.plan_steps:
            self._plan_steps = list(res.plan_steps)
            table = Table(show_header=True, header_style="bold")
            table.add_column("id", style="cyan", no_wrap=True)
            table.add_column("title")
            table.add_column("status", style="yellow")
            for s in res.plan_steps:
                table.add_row(getattr(s, "id", "?"), getattr(s, "title", ""), getattr(s, "status", ""))
            parts.append(table)
        body = Group(*parts) if parts else Text("(空计划)")
        self._mount(_StaticLine(Panel(body, title="📋 计划", border_style="magenta")))

    def notify(self, message: str) -> None:
        self._mount(_StaticLine(f"[dim]{message}[/dim]"))

    def show_skills(self, specs: list) -> None:
        self._mount(_StaticLine(_specs_panel(specs, "🧩 已注册 Skill", "name")))

    def show_agents(self, specs: list) -> None:
        self._mount(_StaticLine(_specs_panel(specs, "🤖 已注册 Subagent 类型", "name")))

    def report_usage(self, usage: dict[str, int] | None, answer: str | None = None) -> None:
        if usage:
            body = "  ".join(
                f"■ {k}={v}"
                for k, v in (
                    ("prompt", usage.get("prompt_tokens", 0)),
                    ("completion", usage.get("completion_tokens", 0)),
                    ("total", usage.get("total_tokens", 0)),
                    ("reasoning", usage.get("reasoning_tokens", 0)),
                    ("cache_hit", usage.get("prompt_cache_hit_tokens", 0)),
                    ("cache_miss", usage.get("prompt_cache_miss_tokens", 0)),
                )
            )
            self._mount(_StaticLine(Panel(body, title="📊 tokens", border_style="bright_black")))
        elif answer:
            est = _estimate_tokens(answer)
            self._mount(
                _StaticLine(
                    Panel(
                        f"[dim]模型未返回用量；输出估算≈{est} tokens[/dim]",
                        title="📊 tokens",
                        border_style="bright_black",
                    )
                )
            )


def _specs_panel(specs: list, title: str, name_key: str) -> Panel:
    """把 Skill/Subagent 精简列表渲染为表格面板。"""
    table = Table(show_header=True, header_style="bold")
    table.add_column("name", style="cyan", no_wrap=True)
    table.add_column("description")
    for s in specs:
        table.add_row(getattr(s, name_key, "?"), (getattr(s, "description", "") or "")[:80])
    return Panel(table, title=title, border_style="blue")


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算（与 agent.context.tokens 解耦，避免引入重依赖）。"""
    return max(1, len(text) // 4)
