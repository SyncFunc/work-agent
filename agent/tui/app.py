"""Textual 全屏 chat 应用（M8）。

架构铁律（见 `knowledge/调研-Textual全屏CLI重构方案.md`）：
- 本类只做「渲染事件 + 收集输入 + HITL 模态」，**不含任何 loop / 工具逻辑**。
- 真正推理由 ``Session`` 在独立 worker 线程跑 ``Session.step`` 驱动（M8.2），
  事件经 ``TextualTransport`` 用 ``app.call_from_thread`` 安全桥接回主线程。
- ``AgentTransport`` 协议 / ``EventStream`` / loop / daemon 协议**一字不改**。
- 渲染逻辑（流式文本 / 工具块 / 计划进度）抽到 ``MessageRenderer`` 复用，主区与子 agent 块共用。
"""

from __future__ import annotations

import asyncio
import queue
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.command import CommandPalette, Hit, Hits, Provider
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Static, TextArea

from agent.config.settings import ui_theme
from agent.core.session_command import _HELP_ROWS, dispatch_command
from agent.runtime.textual_transport import TextualTransport
from agent.tui.render import MessageRenderer, SubagentBlock, _StaticLine

if TYPE_CHECKING:
    from agent.config.settings import Settings
    from agent.core.session import Session


_SENTINEL = object()  # 任务队列哨兵：通知 worker 线程退出


class AgentCommandProvider(Provider):
    """命令面板提供器（M8.5/M8.8）：注册 /plan /exec /skills /compact /context，
    并额外提供「📑 命令目录」入口，选择后展示全部可用命令。

    回调统一转发到 ``ChatApp._handle_command``（经 ``call_later`` 回到主线程安全执行）。
    """

    _DIRECTORY = "📑 命令目录"

    _COMMANDS = (
        (_DIRECTORY, "查看全部可用命令"),
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
            if name == self._DIRECTORY:
                self.app.call_later(self.app._show_command_directory)
            else:
                self.app.call_later(self.app._handle_command, name)

        return run


class ChatInput(TextArea):
    """输入框（TextArea 子类）：当命令提示下拉打开时，拦截方向键 / 回车 / Esc 用于选择。

    拦截逻辑只在提示打开时生效；关闭时完全退回 TextArea 默认行为（光标移动 / 换行 / 缩进）。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    async def _on_key(self, event: Any) -> None:
        app = self.app
        if getattr(app, "_hint_open", False):
            if event.key == "enter":
                app.accept_hint()
                event.stop()
                event.prevent_default()
                return
            if event.key == "escape":
                app.close_hints()
                event.stop()
                event.prevent_default()
                return
            if event.key == "up":
                app.move_hint(-1)
                event.stop()
                event.prevent_default()
                return
            if event.key == "down":
                app.move_hint(1)
                event.stop()
                event.prevent_default()
                return
        await super()._on_key(event)

    def action_cursor_up(self) -> None:
        app = self.app
        if getattr(app, "_hint_open", False):
            app.move_hint(-1)
            return
        super().action_cursor_up()

    def action_cursor_down(self) -> None:
        app = self.app
        if getattr(app, "_hint_open", False):
            app.move_hint(1)
            return
        super().action_cursor_down()


class ChatApp(MessageRenderer, App):
    """全屏 chat 应用：顶部状态栏 + 滚动消息区 + 命令提示下拉 + 底部输入 + 快捷键栏。"""

    CSS_PATH = [Path(__file__).parent / "tui.tcss"]

    CSS = """
    #log { height: 1fr; }
    #input { height: 5; }
    #cmd_hint { display: none; }
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
        # 子 agent 块：name -> SubagentBlock
        self._subagent_blocks: dict[str, SubagentBlock] = {}
        # 命令提示下拉状态
        self._hint_open = False
        self._hint_items: list[tuple[str, str]] = []
        self._hint_index = 0
        self._hint_box: Any = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="log")
        yield VerticalScroll(id="cmd_hint")
        yield ChatInput(id="input", theme="css", tab_behavior="indent")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Agent · 全屏会话"
        self._init_render_state()
        # 主题（M8.5）：从 settings.ui.theme 取 Textual 主题名，落回默认暗色。
        theme = ui_theme(self.settings.ui.theme if self.settings else None)
        try:
            self.theme = theme
        except Exception:
            self.theme = "textual-dark"
        # 缓存日志区引用：worker 线程经 call_from_thread 高频挂载部件，
        # 每次 query_one 在并发回调下偶发 NoMatches（屏幕就绪竞态），缓存后稳定。
        self._log = self.query_one("#log", VerticalScroll)
        self._hint_box = self.query_one("#cmd_hint", VerticalScroll)
        self._mount(
            _StaticLine(
                Text(
                    "欢迎使用全屏 chat（Ctrl+Q 退出；Ctrl+J 发送；Ctrl+P 命令面板；"
                    "Ctrl+↑/↓ 浏览历史；输入 / 查看命令；写入大文件时已自动折叠/截断）。"
                )
            )
        )
        # 顶部状态栏：周期刷新 ctx%（set_interval 在 app 主线程触发，可直接更新 Header）。
        self.set_interval(1.0, self._refresh_ctx)
        # 有 session 时：创建 transport 并启动 worker 线程驱动 Session.step（方案 B）。
        if self.session is not None:
            self.transport = TextualTransport(self, context_mgr=self.session.context_mgr)
            self._start_driver()
        self.query_one(ChatInput).focus()

    def action_open_commands(self) -> None:
        """Ctrl+P：打开命令面板（手动注入 AgentCommandProvider，绕过 App.COMMANDS）。"""
        self.push_screen(CommandPalette(providers=[AgentCommandProvider]))

    # ------------------------------------------------------------------ #
    # 命令提示下拉（M8.8）：输入 / 时显示可选命令/skill 列表
    # ------------------------------------------------------------------ #
    def _command_candidates(self) -> list[tuple[str, str]]:
        cands: list[tuple[str, str]] = list(_HELP_ROWS)
        # 动态补充已注册 Skill
        if self.session is not None and hasattr(self.session, "list_skills"):
            try:
                for s in self.session.list_skills():
                    name = getattr(s, "name", "?")
                    desc = (getattr(s, "description", "") or "")[:60]
                    cands.append((f"/skill {name}", desc))
            except Exception:
                pass
        return cands

    def _update_hints(self) -> None:
        ta = self.query_one(ChatInput)
        line = ta.text.split("\n", 1)[0]
        if not line.startswith("/") or " " in line:
            self.close_hints()
            return
        q = line.lower()
        matches = [(c, d) for c, d in self._command_candidates() if c.lower().startswith(q)]
        if not matches:
            self.close_hints()
            return
        self._hint_items = matches[:12]
        self._hint_index = 0
        self._render_hint()
        self._hint_box.display = True
        self._hint_open = True

    def _render_hint(self) -> None:
        self._hint_box.remove_children()
        for i, (c, d) in enumerate(self._hint_items):
            cls = "hint-item -active" if i == self._hint_index else "hint-item"
            item = Static(f"{c}  {d}", classes=cls)
            item.can_focus = False
            idx = i
            item.on_click = lambda e, idx=idx: self.accept_hint(idx)  # type: ignore[assignment]
            self._hint_box.mount(item)

    def accept_hint(self, i: int | None = None) -> None:
        if not self._hint_open:
            return
        if i is None:
            i = self._hint_index
        if i < 0 or i >= len(self._hint_items):
            return
        cmd, _ = self._hint_items[i]
        ta = self.query_one(ChatInput)
        ta.text = cmd + " "
        last_row = ta.document.line_count - 1
        ta.cursor_location = (last_row, len(ta.document[last_row]))
        self.close_hints()
        ta.focus()

    def move_hint(self, delta: int) -> None:
        if not self._hint_open or not self._hint_items:
            return
        self._hint_index = (self._hint_index + delta) % len(self._hint_items)
        self._render_hint()

    def close_hints(self) -> None:
        self._hint_open = False
        if self._hint_box is not None:
            self._hint_box.display = False

    def on_text_area_changed(self, event: Any) -> None:
        self._update_hints()

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
    # 子 agent 独立块（M8.8）
    # ------------------------------------------------------------------ #
    def ensure_subagent_block(self, name: str) -> SubagentBlock:
        """取得（或创建并挂载）某个子 agent 的专属块；幂等。"""
        if name not in self._subagent_blocks:
            blk = SubagentBlock(name)
            self._subagent_blocks[name] = blk
            self._mount(blk)
        return self._subagent_blocks[name]

    # ------------------------------------------------------------------ #
    # 输入与 chat 循环（方案 B：thread worker 跑 Session.step）
    # ------------------------------------------------------------------ #
    async def action_submit(self) -> None:
        """Ctrl+J：提交输入。空输入忽略；exit/quit 退出；/ 命令走 dispatch_command；
        其余作为任务投递给 worker 线程跑 Session.step。"""
        self.close_hints()
        ta = self.query_one(ChatInput)
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

    def _show_command_directory(self) -> None:
        """Ctrl+P 命令目录：在主区展示全部可用命令（复用 /help 输出）。"""
        self.run_worker(self._handle_command("/help"), exclusive=False)

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
