"""全屏 TUI 的 HITL 模态屏（M8.3）。

设计（见 `knowledge/调研-Textual全屏CLI重构方案.md` §4.2）：
- HITL 由 ``TextualTransport.ask/approve/confirm_plan`` 在 **worker 线程**触发；
  方法内创建一个 ``concurrent.futures.Future``，经 ``app.call_from_thread`` 把对应模态屏
  推到 **Textual 主线程**；用户操作后由屏幕（主线程）``Future.set_result`` 唤醒被 await 的 step。
- ``Future`` 是 ``concurrent.futures.Future``，跨线程 ``set_result`` 线程安全；worker 线程用
  ``asyncio.wrap_future`` 挂回自己的事件循环（与 app 主线程是两个不同 loop）。
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.screen import ModalScreen
from textual.widgets import Input, Static


class AskScreen(ModalScreen):
    """澄清提问屏：有选项时数字键选择；无选项时自由输入（Enter 提交）。"""

    def __init__(self, question: Any, future: Any) -> None:
        super().__init__()
        self._question = question
        self._future = future
        self._options = list(question.options or [])
        self._multi = bool(question.multiSelect)
        self._selected: set[int] = set()

    def compose(self):
        yield Static(self._header(), id="q")
        if self._options:
            yield Static(self._options_text(), id="opts")
        else:
            yield Input(placeholder="输入回答后回车…", id="inp")

    def on_mount(self) -> None:
        if not self._options:
            self.query_one(Input).focus()

    def _header(self) -> Text:
        return Text.from_markup(f"❓ [bold]{self._question.question}[/bold]")

    def _options_text(self) -> Text:
        lines = []
        for i, o in enumerate(self._options, 1):
            mark = "✓" if (i - 1) in self._selected else " "
            lines.append(f"  [{mark}] {i}. {o}")
        hint = "（数字键选择；多选取空格切换，回车确认）" if self._multi else "（数字键选择）"
        return Text("\n".join(lines) + f"\n[dim]{hint}[/dim]")

    def _refresh_options(self) -> None:
        self.query_one("#opts", Static).update(self._options_text())

    def _finish(self, value: str) -> None:
        if not self._future.done():
            self._future.set_result(value)
        self.dismiss()

    def on_key(self, event) -> None:
        if not self._options:
            return  # 自由文本：交给 Input 处理 Enter
        if event.key.isdigit():
            idx = int(event.key) - 1
            if 0 <= idx < len(self._options):
                if self._multi:
                    self._selected.discard(idx) if idx in self._selected else self._selected.add(
                        idx
                    )
                    self._refresh_options()
                else:
                    self._finish(self._options[idx])
        elif event.key == "enter":
            if self._multi:
                vals = [self._options[i] for i in sorted(self._selected)]
                self._finish(", ".join(vals))
            elif self._selected:
                self._finish(self._options[sorted(self._selected)[0]])
            elif self._options:
                self._finish(self._options[0])
        elif event.key == "escape":
            self._finish("")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._finish(event.value)


class ApproveScreen(ModalScreen):
    """审批屏：展示待审批操作，y/N 确认。"""

    def __init__(self, action: Any, future: Any) -> None:
        super().__init__()
        self._action = action
        self._future = future

    def compose(self):
        risk = self._action.risk or "?"
        body = (
            f"[bold]{self._action.tool}[/bold]\n"
            f"[dim]{self._action.description}[/dim]\n\n"
            f"风险等级: {risk}"
        )
        if self._action.approval_request:
            body += "\n[yellow]模型主动请求审批[/yellow]"
        yield Static(Text.from_markup(body), id="approve")

    def _finish(self, value: bool) -> None:
        if not self._future.done():
            self._future.set_result(value)
        self.dismiss()

    def on_key(self, event) -> None:
        if event.key in ("y", "Y"):
            self._finish(True)
        elif event.key in ("n", "N"):
            self._finish(False)
        elif event.key == "escape":
            self._finish(False)


class PlanScreen(ModalScreen):
    """计划确认屏：展示简短提示，y/N 确认执行计划。

    （计划正文已由 ``show_plan`` 事件渲染到主消息区，这里只做确认交互。）
    """

    def __init__(self, future: Any) -> None:
        super().__init__()
        self._future = future

    def compose(self):
        yield Static(
            Text.from_markup("📋 [bold]是否执行该计划？[/bold]  [dim](y/N)[/dim]"),
            id="plan",
        )

    def _finish(self, value: bool) -> None:
        if not self._future.done():
            self._future.set_result(value)
        self.dismiss()

    def on_key(self, event) -> None:
        if event.key in ("y", "Y"):
            self._finish(True)
        elif event.key in ("n", "N"):
            self._finish(False)
        elif event.key == "escape":
            self._finish(False)
