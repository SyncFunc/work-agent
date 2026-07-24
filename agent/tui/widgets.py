"""全屏 TUI 部件（M8）：消息流中的各类型部件。

部件只负责「展示」；数据来自 ``TextualTransport`` 经 ``app.call_from_thread`` 推来的事件。
- 流式文本（``AssistantMessage`` / ``ReasoningMessage``）增量更新并**节流**（coalesce）：
  多次 `append` 合并到下一个 loop tick 再整段重渲染，避免每次增量整段重解析（O(n²)）。
- 工具块 ``ToolBlock`` 继承 ``Collapsible``，展开显示参数 JSON 高亮 + 结果 diff/Markdown 高亮，
  对标 Claude Code 的工具调用体验（调研 §2）。类名保持稳定，便于测试按类型断言。

注意：渲染方法命名为 ``_build`` 而非 ``_render`` —— ``Static`` 内部已有 ``_render()``，
重写会破坏 Textual 的布局测量（高度计算调用 ``self._render()``）。
"""

from __future__ import annotations

from typing import Any

from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from textual.binding import Binding
from textual.widgets import Collapsible, Static


def _truncate(text: str, limit: int = 6000) -> str:
    """超大内容截断，避免单个 Syntax/Markdown 部件过重导致 UI 卡顿（写入大文件时尤其明显）。"""
    if text and len(text) > limit:
        return text[:limit] + f"\n…(已截断，原文共 {len(text)} 字符)"
    return text


class UserMessage(Static):
    """用户消息：左侧竖线 + `›` 前缀。"""

    DEFAULT_CSS = "UserMessage { border-left: wide $accent; padding: 0 1; margin: 0 1 1 1; }"

    def __init__(self, text: str) -> None:
        super().__init__(Text(f"› {text}"))


class _StreamingMessage(Static):
    """流式文本的基类：coalesce 节流——`append` 只累加文本，渲染推迟到下一次 loop tick。"""

    def __init__(self, text: str = "") -> None:
        self._full = text
        self._dirty = False  # 是否有未刷出的增量
        self._render_len = 0  # 上次已渲染文本长度（用于阈值判断）
        super().__init__(self._build(text) if text else self._placeholder())

    # ---- 子类需实现的渲染 ----
    def _build(self, text: str):
        raise NotImplementedError

    def _placeholder(self):
        return Text("")

    # ---- 流式接口 ----
    def append(self, chunk: str) -> None:
        self._full += chunk
        self._schedule_flush()

    def set(self, text: str) -> None:
        self._full = text
        self._render_len = len(self._full)
        self.update(self._build(self._full))

    def _schedule_flush(self) -> None:
        """coalesce：已排程则跳过；否则在下一个 loop tick 刷一次。"""
        if self._dirty:
            return
        self._dirty = True
        try:
            self.app.call_later(self._flush)
        except Exception:
            # 极端情况下（无 app / 未挂载）直接同步刷，保证文本不丢
            self._flush()

    def _flush(self) -> None:
        self._dirty = False
        self._render_len = len(self._full)
        self.update(self._build(self._full))

    def flush(self) -> None:
        """定稿：把剩余未刷增量一次性渲染（loop 一轮结束调用）。"""
        if self._dirty or self._render_len != len(self._full):
            self._flush()

    @property
    def full(self) -> str:
        return self._full


class AssistantMessage(_StreamingMessage):
    """助手消息：流式 Markdown。"""

    DEFAULT_CSS = "AssistantMessage { margin: 0 1 1 1; }"

    def _build(self, text: str):
        return Markdown(text) if text else Text("")

    def _placeholder(self):
        return Text("")


class ReasoningMessage(_StreamingMessage):
    """思考（reasoning）：暗色增量文本，前缀 💭。"""

    DEFAULT_CSS = "ReasoningMessage { margin: 0 1 1 1; color: $text-muted; }"

    def _build(self, text: str):
        return Text("💭 " + text) if text else Text("💭")

    def _placeholder(self):
        return Text("💭")


class ToolBlock(Collapsible):
    """可折叠工具块（M8.4/M8.8）：标题 🔧 name。

    - 参数（`Syntax(json)`）作为 ``Collapsible`` 的子部件，结果同样挂入 ``Contents`` 容器，
      **参数与结果分离且整体可折叠**（修复旧实现把结果挂到 Collapsible 直接子节点导致无法折叠）。
    - 超大结果/diff 截断（`_truncate`），避免写入大文件时单部件过重导致滚动卡顿。
    - 暴露 ``space`` / ``enter`` 折叠快捷键，并 ``can_focus`` 以便键盘操作。
    """

    DEFAULT_CSS = "ToolBlock { margin: 0 1 1 1; }"
    BINDINGS = [
        Binding("space", "toggle_collapse", "折叠/展开", show=False),
        Binding("enter", "toggle_collapse", "折叠/展开", show=False),
    ]

    def __init__(self, name: str, args: str) -> None:
        self._name = name
        self._args = args
        self._result_widget: Static | None = None
        self._contents: Any = None
        # 参数作为 Collapsible 的子部件（进入 Contents 容器，随折叠一起显隐）
        params = Static(Syntax(args, "json", theme="ansi_dark", word_wrap=True))
        params.border_title = "参数"
        super().__init__(params, title=f"🔧 {name}", collapsed=False, classes="tool-block")

    def on_mount(self) -> None:
        self._contents = self.query_one(Collapsible.Contents)
        # 冲刷挂载竞态期间暂存的结果（理论上 TOOL_RESULT 晚于挂载，双保险）
        pending = getattr(self, "_pending_result", None)
        if pending is not None:
            self._pending_result = None
            self.set_result(*pending)

    def _ensure_contents(self) -> Any:
        if self._contents is None:
            try:
                self._contents = self.query_one(Collapsible.Contents)
            except Exception:
                self._contents = None
        return self._contents

    def set_result(self, result_text: str, ok: bool, diff: str | None = None) -> None:
        if diff:
            body: Any = Syntax(_truncate(diff), "diff", theme="ansi_dark", word_wrap=True)
        elif result_text:
            body = Markdown(_truncate(result_text))
        else:
            body = Text("(空结果)")
        contents = self._ensure_contents()
        if contents is None:
            # 尚未挂载完成，暂存，等 on_mount 后由调用方重新 set_result 时再挂（幂等）
            self._pending_result = (result_text, ok, diff)
            return
        if self._result_widget is None:
            self._result_widget = Static(body)
            self._result_widget.border_title = f"[{'✅' if ok else '❌'}] 结果"
            contents.mount(self._result_widget)
        else:
            self._result_widget.update(body)
            self._result_widget.border_title = f"[{'✅' if ok else '❌'}] 结果"

    def action_toggle_collapse(self) -> None:
        self.collapsed = not self.collapsed
