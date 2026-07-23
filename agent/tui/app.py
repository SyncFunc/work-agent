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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.command import CommandPalette, Hit, Hits, Provider
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Static, TextArea

from agent.config.settings import ui_theme
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


class AgentCommandProvider(Provider):
    """命令面板提供器（M8.5）：注册 /plan /exec /skills /compact /context。

    回调统一转发到 ``ChatApp._handle_command``（经 ``call_later`` 回到主线程安全执行）。
    """

    _COMMANDS = (
        ("/plan", "进入计划模式"),
        ("/exec", "执行 shell 命令"),
        ("/skills", "列出已注册 Skill"),
        ("/compact", "手动压缩上下文"),
        ("/context", "查看上下文用量"),
    )

    async def search(self, query: str) -> Hits:
        q = query.lower().strip().lstrip("/")
        for name, desc in self._COMMANDS:
            if q == "" or q in name.lower().lstrip("/"):
                yield Hit(1.0, name, self._make_run(name), name, desc)

    async def discover(self) -> Hits:
        for name, desc in self._COMMANDS:
            yield Hit(1.0, name, self._make_run(name), name, desc)

    def _make_run(self, name: str):
        def run() -> None:
            self.app.call_later(self.app._handle_command, name)

        return run


class ChatApp(App):
    """全屏 chat 应用：顶部状态栏 + 滚动消息区 + 底部输入 + 快捷键栏。"""

    CSS_PATH = [Path(__file__).parent / "tui.tcss"]

    CSS = """
    #log { height: 1fr; }
    #input { height: 5; }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        ("ctrl+c", "quit", "退出"),
        ("ctrl+j", "submit", "发送"),
        ("ctrl+p", "open_commands", "命令面板"),
        ("ctrl+up", "scroll_log_up", "上滚记录"),
        ("ctrl+down", "scroll_log_down", "下滚记录"),
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
        # 主题（M8.5）：从 settings.ui.theme 取 Textual 主题名，落回默认暗色。
        theme = ui_theme(self.settings.ui.theme if self.settings else None)
        try:
            self.theme = theme
        except Exception:
            self.theme = "textual-dark"
        # 缓存日志区引用：worker 线程经 call_from_thread 高频挂载部件，
        # 每次 query_one 在并发回调下偶发 NoMatches（屏幕就绪竞态），缓存后稳定。
        self._log = self.query_one("#log", VerticalScroll)
        self._mount(
            UserMessage(
                "欢迎使用全屏 chat（Ctrl+Q 退出；Ctrl+J 发送；Ctrl+P 命令面板；"
                "Ctrl+↑/↓ 浏览历史；输入 / 或 /help 查看命令）。"
            )
        )
        # 顶部状态栏：周期刷新 ctx%（set_interval 在 app 主线程触发，可直接更新 Header）。
        self.set_interval(1.0, self._refresh_ctx)
        # 有 session 时：创建 transport 并启动 worker 线程驱动 Session.step（方案 B）。
        if self.session is not None:
            self.transport = TextualTransport(self, context_mgr=self.session.context_mgr)
            self._start_driver()
        self.query_one(TextArea).focus()

    def action_open_commands(self) -> None:
        """Ctrl+P：打开命令面板（手动注入 AgentCommandProvider，绕过 App.COMMANDS）。"""
        self.push_screen(CommandPalette(providers=[AgentCommandProvider]))

    def action_scroll_log_up(self) -> None:
        """Ctrl+↑：向上浏览聊天记录（TextArea 占用 up/down/pageup，故用 ctrl 组合键）。"""
        self._log.scroll_relative(y=-max(1, self._log.size.height - 2), animate=False)

    def action_scroll_log_down(self) -> None:
        """Ctrl+↓：向下浏览聊天记录。"""
        self._log.scroll_relative(y=max(1, self._log.size.height - 2), animate=False)

    def _refresh_ctx(self) -> None:
        """周期刷新顶部状态栏的 ctx% 副标题（来自 ContextManager.estimate_usage）。"""
        pct = ""
        try:
            cm = self.session.context_mgr if self.session is not None else None
            if cm is not None and hasattr(cm, "estimate_usage"):
                usage = cm.estimate_usage()
                used = float(getattr(usage, "used_pct", 0.0) or 0.0)
                pct = f"ctx: {int(round(used * 100))}%"
        except Exception:
            pct = ""
        if self.sub_title != pct:
            self.sub_title = pct

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
                res = None
            else:
                if res is not None:
                    self.transport.report_usage(res.usage, res.text)
            # 一轮结束：收尾 transport（退订当前流、刷通知）
            self.transport.close()

    # ------------------------------------------------------------------ #
    # 内部工具：挂载部件 + 自动吸底（#log 引用在 on_mount 缓存，避免并发查询竞态）
    # ------------------------------------------------------------------ #
    def _mount(self, widget: Any) -> None:
        # 仅当用户已停在底部时才自动吸底；否则保留其浏览历史的滚动位置。
        at_bottom = self._log.scroll_offset.y >= (self._log.max_scroll_y - 1)
        self._log.mount(widget)
        if at_bottom:
            self._log.scroll_end(animate=False)

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
            block.set_result(res.output or res.error or "", res.ok, res.diff)

    def append_plan_progress(self, ev: Any) -> None:
        upd = ev.plan_update or {}
        line = f"📋 计划进度: {upd.get('step_id', '?')} → {upd.get('status', '?')}"
        if upd.get("note"):
            line += f"  ({upd['note']})"
        self._mount(_StaticLine(line))

    def finalize_stream(self) -> None:
        # 一轮决策结束：定稿当前流式块（把剩余未刷增量一次性渲染），下次文本另起新块
        if self._current is not None:
            self._current.flush()
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
                table.add_row(
                    getattr(s, "id", "?"), getattr(s, "title", ""), getattr(s, "status", "")
                )
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
