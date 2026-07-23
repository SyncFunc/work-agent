"""全屏 TUI 部件（M8）：消息流中的各类型部件。

部件只负责「展示」；数据来自 ``TextualTransport`` 经 ``app.call_from_thread`` 推来的事件。
流式文本通过 ``append`` 增量更新（M8.4 再加节流）。类名在 M8.4 演进（ToolBlock 内部改
Collapsible + Syntax），但对外类名保持稳定，便于测试按类型断言。
"""

from __future__ import annotations

import json

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from textual.widgets import Collapsible, Static


class UserMessage(Static):
    """用户消息：左侧竖线 + `›` 前缀。"""

    DEFAULT_CSS = "UserMessage { border-left: wide $accent; padding: 0 1; margin: 0 1 1 1; }"

    def __init__(self, text: str) -> None:
        super().__init__(Text(f"› {text}"))


class AssistantMessage(Static):
    """助手消息：流式 Markdown；``append`` 增量更新当前块。"""

    DEFAULT_CSS = "AssistantMessage { margin: 0 1 1 1; }"

    def __init__(self, text: str = "") -> None:
        self._full = text
        super().__init__(Markdown(text) if text else Text(""))

    def append(self, chunk: str) -> None:
        self._full += chunk
        self.update(Markdown(self._full))

    def set(self, text: str) -> None:
        self._full = text
        self.update(Markdown(self._full))

    @property
    def full(self) -> str:
        return self._full


class ReasoningMessage(Static):
    """思考（reasoning）：暗色增量文本，前缀 💭。"""

    DEFAULT_CSS = "ReasoningMessage { margin: 0 1 1 1; color: $text-muted; }"

    def __init__(self, text: str = "") -> None:
        self._full = text
        super().__init__(Text("💭 " + text) if text else Text("💭"))

    def append(self, chunk: str) -> None:
        self._full += chunk
        self.update(Text("💭 " + self._full))


class ToolBlock(Static):
    """工具调用/结果块：M8.1 用 Panel 即时展示；M8.4 内部升级为 Collapsible + Syntax 高亮。

    类名保持稳定，测试按类型断言。
    """

    DEFAULT_CSS = "ToolBlock { margin: 0 1 1 1; }"

    def __init__(self, name: str, args: str) -> None:
        self._name = name
        super().__init__(Panel(args, title=f"🔧 {name}", border_style="cyan"))

    def set_result(self, result_text: str, ok: bool) -> None:
        style = "green" if ok else "red"
        body = result_text or ""
        self.update(
            Panel(
                Markdown(body) if body else Text("(空结果)"),
                title=f"[{'✅' if ok else '❌'}] {self._name}",
                border_style=style,
            )
        )


class CollapsibleToolBlock(Collapsible):
    """M8.4：可折叠工具块（标题 🔧 name，展开显示参数 JSON + 结果 diff/Markdown）。

    继承 ``Collapsible`` 以获得原生折叠交互；同时保留 ``set_result`` 接口供 transport 更新。
    """

    def __init__(self, name: str, args: str) -> None:
        from rich.syntax import Syntax

        self._name = name
        super().__init__(title=f"🔧 {name}", classes="tool-block")
        self._args_syntax = Syntax(args, "json", theme="ansi_dark", word_wrap=True)
        self._result_widget: Static | None = None

    def set_result(self, result_text: str, ok: bool, diff: str | None = None) -> None:
        from rich.syntax import Syntax

        if diff:
            body = Syntax(diff, "diff", theme="ansi_dark", word_wrap=True)
        elif result_text:
            body = Markdown(result_text)
        else:
            body = Text("(空结果)")
        self._result_widget = Static(body)
        self._result_widget.border_title = f"[{'✅' if ok else '❌'}] 结果"
        self.mount(self._result_widget)
