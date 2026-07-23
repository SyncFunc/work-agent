"""Textual 全屏 chat 应用（M8）。

架构铁律（见 `knowledge/调研-Textual全屏CLI重构方案.md`）：
- 本类只做「渲染事件 + 收集输入 + HITL 模态」，**不含任何 loop / 工具逻辑**。
- 真正推理由 ``Session`` 在独立 worker 线程跑 ``Session.step`` 驱动（M8.2），
  事件经 ``TextualTransport`` 用 ``app.call_from_thread`` 安全桥接回主线程。
- ``AgentTransport`` 协议 / ``EventStream`` / loop / daemon 协议**一字不改**。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Header, RichLog, TextArea

if TYPE_CHECKING:
    from agent.config.settings import Settings
    from agent.core.session import Session
    from agent.runtime.textual_transport import TextualTransport


class ChatApp(App):
    """全屏 chat 应用：顶部状态栏 + 滚动消息区 + 底部输入 + 快捷键栏。"""

    CSS = """
    #log { height: 1fr; }
    #input { height: 5; }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        ("ctrl+c", "quit", "退出"),
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

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield RichLog(id="log", markup=True, wrap=True, auto_scroll=True)
            yield TextArea(id="input", theme="css", tab_behavior="indent")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Agent · 全屏会话"
        log = self.query_one(RichLog)
        log.write("欢迎使用全屏 chat（Ctrl+Q 退出）。")
