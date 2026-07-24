"""可复用的消息渲染 mixin（M8.8+）：主聊天区 ``ChatApp`` 与子 agent 块 ``SubagentBlock`` 共用，
避免重复流式/工具块渲染逻辑。

契约（子类必须提供）：
- ``self._log_container``：消息挂载目标（``VerticalScroll`` 或内部滚动容器），在 ``on_mount`` 设置。
- ``self._mount(widget)``：子类实现的挂载方法（``ChatApp`` 带自动吸底；``SubagentBlock`` 直接挂内部容器）。
- ``self._init_render_state()``：初始化流式状态（``_current`` / ``_tool_blocks`` 等）。
"""

from __future__ import annotations

from typing import Any

from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container, VerticalScroll
from textual.widgets import Static

from agent.tui.widgets import AssistantMessage, ReasoningMessage, ToolBlock, UserMessage


class _StaticLine(Static):
    """把任意 Rich renderable 包成一个可挂载的静态行。"""


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算（与 agent.context.tokens 解耦，避免引入重依赖）。"""
    return max(1, len(text) // 4)


def _specs_panel(specs: list, title: str, name_key: str) -> Panel:
    """把 Skill/Subagent 精简列表渲染为表格面板。"""
    table = Table(show_header=True, header_style="bold")
    table.add_column("name", style="cyan", no_wrap=True)
    table.add_column("description")
    for s in specs:
        table.add_row(getattr(s, name_key, "?"), (getattr(s, "description", "") or "")[:80])
    return Panel(table, title=title, border_style="blue")


class MessageRenderer:
    """流式文本 / 工具块 / 计划进度 等渲染逻辑（与主区、子 agent 块共用）。"""

    # ------------------------------------------------------------------ #
    # 子类工具：流式状态初始化 + 挂载
    # ------------------------------------------------------------------ #
    def _init_render_state(self) -> None:
        self._current: Any = None  # 当前正在流式更新的消息部件
        self._current_is_reasoning = False
        self._tool_blocks: dict[str, Any] = {}  # tool_call_id -> ToolBlock
        self._plan_steps: list[Any] | None = None

    def _mount(self, widget: Any) -> None:
        """子类实现的挂载方法（``ChatApp`` 带自动吸底；``SubagentBlock`` 直接挂内部容器）。"""
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # 事件 → 部件 渲染（由 Transport 经线程桥接调用）
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
        args = tc.arguments
        if not isinstance(args, str):
            import json

            args = json.dumps(args, ensure_ascii=False, indent=2)
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

    def render_notify(self, message: str) -> None:
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


class SubagentBlock(Container, MessageRenderer):
    """子 agent 的专属块（M8.8）：带标题的独立容器，内部消息流式/工具块渲染与主区一致。

    与「前缀汇入主区」旧实现不同，这里把子 agent 的对话收敛进一个独立块，层级清晰、
    可整体折叠，且内部复用 ``MessageRenderer`` 的渲染逻辑（不重复代码）。
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._log_container: Any = None
        self._pending: list[Any] = []  # 块尚未挂载完成前的暂存部件
        self._init_render_state()
        super().__init__(classes="subagent-block")

    def compose(self) -> ComposeResult:
        yield Static(f"🤖 subagent: {self._name}", classes="subagent-title")
        yield VerticalScroll(id="subagent_log")

    def on_mount(self) -> None:
        self._log_container = self.query_one("#subagent_log", VerticalScroll)
        # 冲刷挂载竞态期间暂存的部件
        if self._pending:
            for w in self._pending:
                self._log_container.mount(w)
            self._pending.clear()

    def _mount(self, widget: Any) -> None:
        if self._log_container is None:
            try:
                self._log_container = self.query_one("#subagent_log", VerticalScroll)
            except Exception:
                self._log_container = None
        if self._log_container is None:
            # 块尚未挂载完成，先暂存，待 on_mount 冲刷
            self._pending.append(widget)
            return
        self._log_container.mount(widget)
