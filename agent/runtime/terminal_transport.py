"""rich 终端传输实现（``TerminalTransport``）。

把 ``AgentTransport`` 协议落到 rich 终端：HITL 交互 + 由 ``EventStream`` 订阅驱动的
实时渲染。本文件**只负责终端呈现**，不应包含任何 CLI 命令编排（命令编排见 ``agent.cli``）。

渲染完全由 ``EventStream`` 订阅驱动（``bind`` 注册 ``_on_event`` sink）：loop 不再回调
任何 presenter，只落事件；本类在 sink 内把 ``text`` / ``tool_use`` / ``tool_call_delta`` /
``tool_result`` / ``plan_progress`` / ``decision`` 等事件翻译成 rich 终端输出。未来做网页
版只需另实现一套 ``AgentTransport``（订阅事件转发 websocket），无需改动 loop / session。

- 思考（reasoning）：暗色增量实时打印（``💭 思考:`` 头 + 逐片文本），不进框。
- 输出（content）：用单个 ``Live`` 渲染**带框的 Markdown 面板**（``💬 模型输出``），流式
  过程中把面板裁剪到屏幕高度内，杜绝内容超高整块重发刷屏；段结束 ``stop()`` 定稿。
- 工具调用 / 结果：用 Panel 即时展示（清晰区分「工具调用」类别）。
- ``report_usage`` 打印 token 用量。

**为什么不重新引入历史刷屏 bug**：每个内容段用独立的 ``_buf``，``Live`` 实例在段开始时
创建、段结束时 ``stop()`` 后丢弃，绝不跨段累积；流式时把面板高度裁到屏幕内，就地刷新
（同高 → 不滚动、不重发）；段结束才 ``stop()`` 渲染一次完整面板（仅一次滚动）。
"""

from __future__ import annotations

import json
from typing import Any

import typer
from prompt_toolkit import PromptSession
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from agent.core.control_tools import (
    ASK_CLARIFICATION_TOOL_NAME,
    UPDATE_PLAN_TOOL_NAME,
    SPAWN_SUBAGENT_TOOL_NAME,
)
from agent.core.events import Event, EventStream
from agent.core.intent import Question
from agent.core.transport import AgentTransport
from agent.runtime.approval import Action


# 写/改类工具名（与 agent.tools.fs 对齐）；其 ToolResult.diff 以高亮面板展示改动。
WRITE_TOOL_NAME = "write"
EDIT_TOOL_NAME = "edit"


# 计划步骤状态 → 展示标记 / 颜色（与 agent.core.plan 的状态对齐）
_PLAN_STATUS_MARK = {
    "pending": "[ ]", "in_progress": "[~]", "done": "[x]",
    "blocked": "[!]", "skipped": "[-]",
}
_PLAN_STATUS_COLOR = {
    "pending": "white", "in_progress": "yellow", "done": "green",
    "blocked": "red", "skipped": "dim",
}


# token 估算共享到 agent.context.tokens（单一事实来源，避免重复实现）
from agent.context.tokens import _estimate_tokens  # noqa: E402


def _extract_write_preview(raw: str) -> str:
    """从累计（可能不完整的）工具参数 JSON 中尽力提取 write/edit 的正文预览。

    优先取 ``content``（write），其次 ``new_string``（edit）。返回已生成的正文片段；
    参数尚未流到正文或 JSON 尚不可解析时返回空串。仅供流式预览，不做严格解析。
    """
    for key in ("content", "new_string"):
        i = raw.find(f'"{key}"')
        if i < 0:
            continue
        col = raw.find(":", i)
        if col < 0:
            return ""
        q = raw.find('"', col)
        if q < 0:
            return ""
        val = raw[q + 1:]
        # 截到首个未转义的双引号（值的闭合引号），兼容流式中间态（尚未出现闭合引号时保留全部）。
        for idx in range(len(val)):
            if val[idx] == '"' and (idx == 0 or val[idx - 1] != "\\"):
                val = val[:idx]
                break
        return val
    return ""


def _render_steps_panel(steps: Any, *, title: str) -> Panel:
    """把步骤列表渲染为带状态色的面板（共享给 plan 展示与进度更新）。"""
    if not steps:
        return Panel("(无步骤)", title=title, border_style="magenta", expand=False)
    lines = []
    for s in steps:
        mark = _PLAN_STATUS_MARK.get(s.status, "[ ]")
        color = _PLAN_STATUS_COLOR.get(s.status, "white")
        lines.append(f"[{color}]{mark} {s.id} — {s.title}[/{color}]")
    return Panel("\n".join(lines), title=title, border_style="magenta", expand=False)


class _PanelSlot:
    """hub 中一个子 agent 面板槽：内容由子 agent 实时更新其 ``panel``。"""

    __slots__ = ("title", "panel")

    def __init__(self, title: str) -> None:
        self.title = title
        self.panel = Panel("(等待子 agent 输出…)", title=title, border_style="dim")


class SubagentPanelHub:
    """顶层唯一的 Live 面板集：任意层级的子 agent 共用，避免并行渲染抢占同一终端。

    原先每个子 agent 各自持有一个 ``Live`` 直接写真实终端，与父传输的内容 Live、与其
    他并行子 agent 的 Live 互相用光标转义序列抢占，导致输出错乱、``▶ subagent`` 边框
    被穿插进父 agent 文本。这里改为：所有子 agent 在 ``register`` 时获得一个 ``_PanelSlot``，
    其事件累积进自身缓冲后通过 ``refresh`` 把全部 slot 以 ``Group`` 重绘进**同一个** ``Live``。
    该 ``Live`` 在首个 slot 注册时惰性启动、最后一个 slot 注销时停止，从而与父传输自身的
    内容 Live 错峰（子 agent 运行期间父内容 Live 已 ``stop()``）。

    面板高度按当前并行子 agent 数动态平分终端高度，保证多子 agent 并行时整体仍落在屏幕内。
    """

    def __init__(self, console: Console) -> None:
        self._console = console
        self._slots: list[_PanelSlot] = []
        self._live: Live | None = None

    def register(self, title: str) -> _PanelSlot:
        slot = _PanelSlot(title)
        self._slots.append(slot)
        self._ensure_started()
        self.refresh()
        return slot

    def unregister(self, slot: _PanelSlot) -> None:
        if slot in self._slots:
            self._slots.remove(slot)
        if not self._slots and self._live is not None:
            self._live.stop()
            self._live = None

    def refresh(self) -> None:
        """重绘所有子 agent 面板（单一 Live，并行安全：事件循环单线程，刷新天然串行）。"""
        if self._live is not None:
            self._live.update(self._render())
            self._live.refresh()

    def stop_all(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._slots.clear()

    def slot_budget(self) -> int:
        """单个 slot 内「内容」可用的高度预算（行），按并行子 agent 数动态平分终端高度。

        子 agent 自身面板按内容自适应高度，仅当内容超过此预算时才裁剪最旧条目，因此并行
        越多每个面板越矮、但不再有「固定高度撑出的大片空行」。
        """
        n = max(1, len(self._slots))
        # 给若干行余量（各面板边框 + hub 自身），避免正好顶满触发滚动
        per = max(3, (self._console.size.height - 2) // n)
        # 减去外层面板自身上下边框（2 行），得到内部内容预算
        return max(1, per - 2)

    def _render(self) -> Group:
        # 各 slot 面板已由 _SubAgentTransport 按内容自适应高度（不再强制固定高度），
        # 这里只负责把全部 slot 以 Group 拼进唯一的 Live。
        return Group(*(s.panel for s in self._slots))

    def _ensure_started(self) -> None:
        if self._live is None:
            self._live = Live(
                self._render(),
                console=self._console,
                auto_refresh=False,
                screen=False,
            )
            self._live.start()


class TerminalTransport(AgentTransport):
    """``AgentTransport`` 的 rich 终端实现：把 HITL 交互与事件流渲染统一到单一契约。"""

    def __init__(self, *, interactive: bool, context_mgr: "Any | None" = None) -> None:
        self._interactive = interactive
        self._console = Console()
        # M4.6：可选上下文管理器，用于状态栏实时显示占用占比。
        self._context_mgr = context_mgr
        self._saw_reasoning = False   # 本思考段是否已打印过 "💭 思考:" 头
        self._live = None             # 当前内容段的 Live（流式 Markdown 面板）
        self._buf = ""                # 当前内容段累积文本
        self._tool_live = None        # 工具调用参数流式预览的 Live（write/edit 内容实时显示）
        self._tc_by_id: dict[str, Any] = {}   # tool_use 事件收集，供 tool_result 取工具名
        self._plan_steps: list[Any] | None = None  # 计划步骤（show_plan 时记录，progress 增量更新）
        # 通知缓冲：notify() 只记录、不渲染；由 flush_notifications() 在不在流式 Live 中的
        # 安全时机统一呈现（见 notify / flush_notifications / close）。
        self._pending_notifications: list[str] = []
        # 渲染缓冲：需要在「不在流式 Live / 不在 ptk 输入行」的安全时机才打印的内容
        # （如后台 Subagent 的最终面板）。由 flush_notifications() 一并刷出。
        self._pending_renderables: list[Any] = []
        # 子 agent 面板集（并行安全）：交互模式下创建一个顶层唯一 Live，所有层级的子 agent
        # 共用；非交互则不创建（子 agent 走降级渲染，见 agent/subagent.py）。
        # 用 _own_hub 记录「本传输自己拥有的 hub」：close 只停自己的，避免子 agent 的
        # subagent_hub 属性（委派到父 hub）误停父 hub、连累其他并行子 agent。
        self._own_hub: SubagentPanelHub | None = (
            SubagentPanelHub(self._console) if interactive else None
        )

    @property
    def interactive(self) -> bool:
        return self._interactive

    @property
    def subagent_hub(self) -> "SubagentPanelHub | None":
        """本传输自身拥有的子 agent 面板 hub。

        子 agent 的 ``_SubAgentTransport`` 同名属性是只读 property，委派到父传输的 hub，
        使任意层级的子 agent 共用顶层唯一 Live；父传输（如主 TerminalTransport）这里直接
        返回自己拥有的 hub。
        """
        return self._own_hub

    # ------------------------------------------------------------------ #
    # 渲染：由 bind 订阅的事件流驱动（取代原 LoopPresenter 回调）
    # ------------------------------------------------------------------ #
    def _status_line(self) -> str:
        """生成状态栏信息（上下文占比），供 chat prompt 前缀展示。

        - 未配置 ``context_mgr`` 时返回空串。
        - 占比 >90% 红、>70% 黄、否则绿（rich 标记）。
        """
        if self._context_mgr is None:
            return ""
        usage = self._context_mgr.estimate_usage()
        pct = usage.used_pct
        color = "red" if pct > 0.9 else ("yellow" if pct > 0.7 else "green")
        return f"ctx: [{color}]{pct:.0%}[/{color}]"

    def bind(self, stream: EventStream) -> None:
        """订阅 EventStream：loop 创建流后即调用，执行期事件实时到达本 sink。"""
        self._tc_by_id = {}
        stream.subscribe(self._on_event)

    def _on_event(self, ev: Event) -> None:
        t = ev.type
        if t == "text":
            self.on_text(ev.text or "", ev.kind or "content")
        elif t == "tool_use":
            if ev.tool_use is not None:
                self._tc_by_id[ev.tool_use.id] = ev.tool_use
                self.on_tool_call(ev.tool_use)
        elif t == "tool_call_delta":
            # 瞬时事件（不入档）：write/edit 参数生成中实时预览
            self.on_tool_call_delta(ev.tc_index or 0, ev.tc_name or "", ev.tc_args or "")
        elif t == "tool_result":
            # tool_result 事件只带 tool_call_id + ToolResult；工具名从 tool_use 收集而来
            if ev.tool_call_id is not None:
                tc = self._tc_by_id.get(ev.tool_call_id)
                if tc is not None and ev.tool_result is not None:
                    self.on_tool_result(tc, ev.tool_result)
        elif t == "plan_progress":
            self.on_plan_progress(ev)
        elif t == "decision":
            # 一轮模型决策结束收尾（澄清/计划闸门提前返回时工具回调不触发，统一在此定稿）
            self._on_decision_done()
        # clarify / plan / final 等由 HITL（show_questions/show_plan）或已流式文本覆盖，忽略

    def _max_lines(self) -> int:
        # 给 Panel 边框/标题留 ~4 行余量，避免正好顶满屏幕触发滚动
        return max(5, self._console.size.height - 4)

    def _render_content(self, buf: str, cap: bool) -> Panel:
        text = buf
        if cap:
            lines = buf.splitlines()
            limit = self._max_lines()
            if len(lines) > limit:
                text = "…(流式预览仅显示最近内容，段结束定稿完整版)\n" + "\n".join(lines[-limit:])
        return Panel(
            Markdown(text),
            title="💬 模型输出",
            border_style="green",
            expand=False,
        )

    def _ensure_live(self) -> None:
        """内容段首片：创建并启动 Live（空面板），后续逐片 refresh。"""
        if self._live is None:
            self._buf = ""
            self._live = Live(
                self._render_content("", cap=False),
                console=self._console,
                auto_refresh=False,
            )
            self._live.start()

    def _refresh_live(self) -> None:
        if self._live is None:
            return
        # cap=True：裁到屏幕内，就地刷新（同高不滚动，杜绝刷屏）
        self._live.update(self._render_content(self._buf, cap=True))
        self._live.refresh()

    def _commit_live(self) -> None:
        """内容段结束：用完整面板 stop()，定稿打印一次（仅一次，不刷屏）。"""
        if self._live is None:
            return
        self._live.update(self._render_content(self._buf, cap=False))
        self._live.stop()  # stop 会把完整面板留在屏幕上
        self._live = None

    def _end_reasoning_segment(self) -> None:
        if self._saw_reasoning:
            self._console.print()
            self._saw_reasoning = False

    def on_text(self, text: str, kind: str) -> None:
        if kind == "reasoning":
            # 思考：暗色增量打印（不进框）；若正在流式输出则先定稿该段
            self._commit_live()
            if not self._saw_reasoning:
                self._console.print("💭 [dim]思考:[/dim] ", end="")
                self._saw_reasoning = True
            self._console.out(text, style="dim", end="")
        else:
            # 正式输出：Live 渲染带框 Markdown，流式过程裁高防刷屏
            self._end_reasoning_segment()
            self._ensure_live()
            self._buf += text
            self._refresh_live()

    def _stop_tool_live(self) -> None:
        """收尾工具调用参数的流式预览 Live（若有）。"""
        if self._tool_live is not None:
            self._tool_live.stop()
            self._tool_live = None

    def on_tool_call(self, tc) -> None:
        self._stop_tool_live()  # 结束参数流式预览，改由最终面板定稿
        self._commit_live()
        self._end_reasoning_segment()
        self._console.print()  # 与上方模型输出分隔
        # update_plan 控制工具：渲染专属「📋 计划更新」面板，清晰展示步骤状态变迁
        if tc.name == UPDATE_PLAN_TOOL_NAME:
            a = tc.arguments or {}
            sid = a.get("step_id", "?")
            st = a.get("status", "?")
            color = _PLAN_STATUS_COLOR.get(st, "white")
            note = a.get("note")
            self._console.print(
                Panel(
                    f"[cyan]{sid}[/cyan] → [{color}]{st}[/{color}]"
                    + (f"\n[dim]注: {note}[/dim]" if note else ""),
                    title="📋 计划更新",
                    border_style="magenta",
                    expand=False,
                )
            )
            return
        args = json.dumps(tc.arguments, ensure_ascii=False, indent=2)
        self._console.print(
            Panel(
                f"[cyan]{tc.name}[/cyan]\n```\n{args}\n```",
                title="🔧 工具调用",
                border_style="cyan",
                expand=False,
            )
        )

    def on_tool_call_delta(self, index, name, args_raw) -> None:
        """工具调用参数流式预览：write/edit 在生成 content 时即显示，避免大段写入无输出。

        ``args_raw`` 是该工具调用累计（可能不完整）的 arguments JSON 字符串；本方法只做
        尽力预览，不依赖其完整可解析。最终结构仍由 ``on_tool_call`` 的定稿面板展示。
        """
        self._commit_live()  # 先定稿可能正在流式的内容面板，避免重叠
        self._end_reasoning_segment()
        # ask_clarification 是控制工具：其参数会立即由澄清面板（ask）呈现，无需再展示
        # 「生成参数中…」占位面板——否则因澄清闸门提前返回、on_tool_call 永不触发，该
        # Live 不被收尾，残留面板会扰乱澄清面板渲染。直接跳过。
        if name == ASK_CLARIFICATION_TOOL_NAME:
            return
        if name in (WRITE_TOOL_NAME, EDIT_TOOL_NAME):
            preview = _extract_write_preview(args_raw or "")
            title = f"✍️ {name} …"
            body = Text(preview) if preview else Text("(等待内容…)", style="dim")
            border = "cyan"
        else:
            title = f"🔧 {name} …" if name else f"🔧 工具调用 #{index} …"
            body = Text("(生成参数中…)", style="dim")
            border = "blue"
        if self._tool_live is None:
            self._tool_live = Live(
                Panel(body, title=title, border_style=border, expand=False),
                console=self._console,
                auto_refresh=False,
            )
            self._tool_live.start()
        else:
            self._tool_live.update(Panel(body, title=title, border_style=border, expand=False))
        self._tool_live.refresh()

    def on_tool_result(self, tc, res) -> None:
        self._commit_live()
        self._end_reasoning_segment()
        # update_plan 结果不单独渲染：其步骤进度已由 on_plan_progress 以步骤列表展示
        if tc.name == UPDATE_PLAN_TOOL_NAME:
            return
        # spawn_subagent 结果不单独渲染：子 agent 已有独立面板展示其摘要；重复打印不仅会
        # 冗余，还可能在并行兄弟子 agent 的 Live 仍激活时抢占终端导致渲染错乱。
        if tc.name == SPAWN_SUBAGENT_TOOL_NAME:
            return
        # write / edit：以高亮 diff 面板流式展示实际改动（old→new），而非仅一句字符数
        if tc.name in (WRITE_TOOL_NAME, EDIT_TOOL_NAME) and res.ok and res.diff:
            diff = res.diff
            dcap = 6000
            truncated = len(diff) > dcap
            if truncated:
                diff = diff[:dcap] + "\n…(diff 已截断)"
            self._console.print(
                Panel(
                    Syntax(diff, "diff", theme="ansi_dark", word_wrap=True),
                    title=f"✅ {tc.name} — {res.output}",
                    border_style="green",
                    expand=False,
                )
            )
            return
        style = "green" if res.ok else "red"
        body = res.output or res.error or ""
        if len(body) > 2000:
            body = body[:2000] + "\n…(已截断)"
        # 结果体按 Markdown 渲染（代码块 / 列表 / 表格等格式可见）
        self._console.print(
            Panel(
                Markdown(body),
                title=f"[{'✅' if res.ok else '❌'}] {tc.name}",
                border_style=style,
                expand=False,
            )
        )

    def on_plan_progress(self, ev: Event) -> None:
        """plan_progress 事件：增量更新本地步骤状态并渲染最新步骤列表（含状态色）。"""
        self._commit_live()
        self._end_reasoning_segment()
        upd = ev.plan_update or {}
        if self._plan_steps:
            for s in self._plan_steps:
                if s.id == upd.get("step_id"):
                    s.status = upd.get("status", s.status)
                    if upd.get("note"):
                        s.note = upd["note"]
                    break
        self._console.print()
        self._console.print(_render_steps_panel(self._plan_steps or [], title="📋 计划进度"))

    def _on_decision_done(self) -> None:
        """一轮模型决策结束的收尾：澄清/计划闸门提前返回时工具回调不触发，统一在此定稿。"""
        self._stop_tool_live()
        self._commit_live()
        self._end_reasoning_segment()

    def close(self) -> None:
        # 收尾任何未闭合的流式内容段（定稿为完整 Markdown 面板）
        self._stop_tool_live()
        self._commit_live()
        self._end_reasoning_segment()
        # 收尾子 agent 面板集（若有未注销的 slot，统一停掉，避免残留 Live 抢占终端）。
        # 仅停本传输自己拥有的 hub（子 agent 的 subagent_hub 是委派属性，会指向父 hub，
        # 不能在此停——否则会误关父 hub 连累其他并行子 agent）。
        if self._own_hub is not None:
            self._own_hub.stop_all()
        # 收尾后再呈现积压通知（此时 Live 已停止，不会打断流式输出）
        self.flush_notifications()


    def report_usage(self, usage: dict[str, int] | None, answer: str | None = None) -> None:
        if usage:
            # 不同颜色正方形色块作为各类 token 的图标；展示全部字段（含为 0 的）
            blocks: list[tuple[str, str, int]] = [
                ("[blue]■[/]", "prompt", usage.get("prompt_tokens", 0)),
                ("[green]■[/]", "completion", usage.get("completion_tokens", 0)),
                ("[yellow]■[/]", "total", usage.get("total_tokens", 0)),
                ("[magenta]■[/]", "reasoning", usage.get("reasoning_tokens", 0)),
                ("[cyan]■[/]", "cache_hit", usage.get("prompt_cache_hit_tokens", 0)),
                ("[red]■[/]", "cache_miss", usage.get("prompt_cache_miss_tokens", 0)),
            ]
            body = "  ".join(f"{b} {name}={val}" for b, name, val in blocks)
            self._console.print(
                Panel(body, title="📊 tokens", border_style="bright_black", expand=False)
            )
        elif answer:
            # 模型未返回用量（如本模型流式不返回 usage）：给出基于输出的粗略估算，保证每轮有 token 信息
            est = _estimate_tokens(answer)
            self._console.print(
                Panel(
                    "[dim]模型未返回用量；输出估算≈"
                    f"{est} tokens（切换支持 usage 的模型如 deepseek-chat 可显示精确值）[/dim]",
                    title="📊 tokens",
                    border_style="bright_black",
                    expand=False,
                )
            )

    # ------------------------------------------------------------------ #
    # HITL：会话编排所需的人机交互（原 SessionUI 部分）
    # ------------------------------------------------------------------ #
    async def ask(self, question: Question) -> str:
        # 先空一行：模型的流式思考/输出以 end="" 收尾、无换行，若不分隔会与澄清面板同行粘连。
        self._console.print()
        if question.options:
            opts = question.options
            body = question.question
            if opts:
                # 选项显式打进面板，确保始终可见（修复「澄清选项不显示」）。
                body += "\n[dim]选项: " + "; ".join(opts) + "[/dim]"
            self._console.print(
                Panel(body, title="❓ 澄清", border_style="yellow", expand=False)
            )
            if question.multiSelect:
                return await _ptk_multi_choice(question)
            return await _ptk_single_choice(question)
        return typer.prompt("\n" + question.question)

    def show_questions(self, questions: list[Question]) -> None:
        # 同 ask：先空一行与上方流式输出分隔。
        self._console.print()
        for q in questions:
            extra = f"\n[dim]选项: {', '.join(q.options)}[/dim]" if q.options else ""
            self._console.print(
                Panel(q.question + extra, title="❓ 澄清", border_style="yellow", expand=False)
            )

    def show_plan(self, res) -> None:
        self._console.print("[bold]── Plan ──[/bold]")
        if res.plan:
            self._console.print(Markdown(res.plan))
        if res.plan_steps:
            self._plan_steps = list(res.plan_steps)  # 记录基线步骤，供 progress 增量更新
            self._console.print(_render_steps_panel(res.plan_steps, title="📋 计划步骤"))
        self._console.print(f"(plan file: {res.plan_path})")

    async def confirm_plan(self) -> bool:
        # 用 prompt_toolkit 的 async 接口：confirm_plan 在 session.step 的 asyncio.run()
        # 事件循环内被 await 调用，必须用 prompt_async（协程），否则同步 prompt() 会在已有
        # 事件循环里再次 asyncio.run()，抛 "asyncio.run() cannot be called from a running
        # event loop"。与澄清 UI（prompt_async）保持一致。
        try:
            session = PromptSession()
            ans = await session.prompt_async("是否执行该计划？ [y/N]: ")
        except (EOFError, KeyboardInterrupt):
            return False
        return ans.strip().lower() in {"y", "yes", "是"}

    def notify(self, message: str) -> None:
        """记录一条通知（不负责渲染）。

        ``notify`` 的语义只是「发出一条通知信号」，不在调用点直接渲染（避免在流式 Live
        进行中打断输出）。真正的呈现由 ``flush_notifications()`` 在不在 Live 中的安全时机完成
        （本类在 ``close()`` 内自动触发；REPL 每轮等待输入前也会触发）。
        """
        self._pending_notifications.append(message)

    def queue_render(self, renderable: Any) -> None:
        """把一条需要安全时机渲染的内容（如后台 Subagent 最终面板）入队。

        后台 Subagent 运行时用户正处于 prompt_toolkit 输入行，直接打印会与输入行争用终端
        导致渲染错乱；故先入队，由 ``flush_notifications()`` 在不在流式 Live / 不在 ptk 输入行
        的安全窗口统一打印。
        """
        self._pending_renderables.append(renderable)

    def flush_notifications(self) -> None:
        """把积压的通知与渲染内容输出到终端（应在不在流式 Live 中的安全时机调用）。"""
        for msg in self._pending_notifications:
            typer.echo(msg, err=True)
        self._pending_notifications.clear()
        for r in self._pending_renderables:
            self._console.print(r)
        self._pending_renderables.clear()

    # ------------------------------------------------------------------ #
    # M5.4：Skill / Subagent 列表面板（仅展示 name+描述等精简信息，不含正文）
    # ------------------------------------------------------------------ #
    def show_skills(self, specs: list) -> None:
        """展示已注册 Skill 列表（name / description / paths / 是否仅手动）。"""
        title = "🧩 已注册 Skill"
        if not specs:
            self._console.print(Panel("(无 skill)", title=title, border_style="blue", expand=False))
            return
        table = Table(show_header=True, header_style="bold", expand=False)
        table.add_column("name", style="cyan", no_wrap=True)
        table.add_column("description")
        table.add_column("paths", style="dim")
        table.add_column("模式", style="yellow")
        for s in specs:
            mode = []
            if getattr(s, "disable_model_invocation", False):
                mode.append("仅手动")
            if not getattr(s, "user_invocable", True):
                mode.append("不可手动")
            paths = ", ".join(getattr(s, "paths", []) or [])
            table.add_row(
                s.name,
                (s.description or "")[:80],
                paths,
                "/".join(mode) if mode else "自动",
            )
        self._console.print(Panel(table, title=title, border_style="blue", expand=False))

    def show_agents(self, specs: list) -> None:
        """展示已注册 Subagent 类型（name / description / tools / model）。"""
        title = "🤖 已注册 Subagent 类型"
        if not specs:
            self._console.print(Panel("(无 subagent)", title=title, border_style="blue", expand=False))
            return
        table = Table(show_header=True, header_style="bold", expand=False)
        table.add_column("name", style="cyan", no_wrap=True)
        table.add_column("description")
        table.add_column("tools", style="green")
        table.add_column("model", style="dim")
        for s in specs:
            tools = getattr(s, "tools", None)
            tools_s = ", ".join(tools) if tools else ("禁用:" + ", ".join(getattr(s, "disallowed_tools", []) or []) if getattr(s, "disallowed_tools", None) else "全部")
            model = getattr(s, "model", None) or "inherit"
            scope = "（内置）" if getattr(s, "builtin", False) else "（自定义）"
            table.add_row(
                s.name + scope,
                (s.description or "")[:80],
                tools_s,
                model,
            )
        self._console.print(Panel(table, title=title, border_style="blue", expand=False))

    async def approve(self, action: "Action") -> bool:
        """审批面板：展示待审批的操作并等待用户 y/N 确认。"""
        self._console.print()
        tool_label = {"bash": "🐚 命令", "read": "📖 读取", "write": "✏️ 写入", "edit": "✏️ 编辑"}.get(
            action.tool, f"🔧 {action.tool}"
        )
        body = f"[bold]{tool_label}[/bold]\n"
        body += f"[dim]{action.description}[/dim]\n"
        if action.risk:
            body += f"\n[cyan]风险等级:[/cyan] {action.risk}"
        if action.approval_request:
            body += "\n[yellow]模型主动请求审批[/yellow]"
        self._console.print(Panel(body.strip(), title="🔒 审批请求", border_style="yellow", expand=False))
        try:
            session = PromptSession()
            ans = await session.prompt_async("是否允许执行？ [y/N]: ")
        except (EOFError, KeyboardInterrupt):
            return False
        return ans.strip().lower() in {"y", "yes", "是"}


# --------------------------------------------------------------------------- #
# _SubAgentTransport：子 agent 子任务视图渲染（继承 TerminalTransport，屏蔽独立 HITL）
# --------------------------------------------------------------------------- #
class _SubAgentTransport(TerminalTransport):
    """子 agent 传输：继承 TerminalTransport 获得 HITL 委托能力，但**不直接打印到真实终端**，
    而是把事件累积为「带框条目」列表，统一由父传输的面板集（hub）以单一 Live 展示。

    相比旧的「整段 Markdown 拼进一个固定高度面板」：
    - 每次工具调用 / 结果 / 模型输出 / 思考都渲染为**独立带框 Panel**（嵌套在外层
      ``▶ subagent`` 面板内），层级清晰，工具与模型输出都有框（修复「工具和模型输出没有框」）。
    - 外层面板**按内容自适应高度**，仅在内容超过按并行数动态预算时才丢弃最旧条目，不再强行
      撑到固定高度 → 不再出现大片空行（修复「始终有空行」）。
    """

    def __init__(
        self,
        parent: "AgentTransport | None", *,
        name: str = "subagent", panel_height: int = 15, live: bool = True,
    ) -> None:
        interactive = bool(parent.interactive) if parent is not None else False
        super().__init__(interactive=interactive)
        self._parent = parent
        self._name = name
        # 仅作信息性保留（旧测试引用）；渲染不再强制固定高度，改用终端动态预算
        self._panel_height = max(3, panel_height)
        # live=True：注册进父 hub，用顶层唯一 Live 实时渲染（前台 subagent 用，此时用户不处于
        #   输入提示行，不会与 prompt_toolkit 争用终端）。
        # live=False：仅累积条目、不渲染；最终面板由 close() 交给父传输在「不在 ptk 输入行」的
        #   安全时机打印（后台 subagent 用，运行时用户正处于输入提示行，直接打印会渲染错乱）。
        self._use_live = live
        self._slot = None
        # 已定稿的带框条目：(rich renderable, 估计渲染行数)
        self._entries: list[tuple[Any, int]] = []
        self._stream_text = ""     # 当前流式模型输出（未定稿）
        self._reasoning_text = ""  # 当前流式思考（未定稿）
        # 仅交互模式且 live=True：把自身面板注册进父 hub（所有层级共用顶层唯一 Live）。
        # 用 getattr 容错：测试用的假父传输可能没有 hub（此时退化为无面板渲染）。
        if live and interactive and parent is not None:
            hub = getattr(parent, "subagent_hub", None)
            if hub is not None:
                self._slot = hub.register(f"▶ subagent: {self._name}")

    @property
    def interactive(self) -> bool:
        return bool(self._parent.interactive) if self._parent is not None else False

    def notify(self, message: str) -> None:
        # 子 agent 的通知委派给父传输：统一进父的缓冲，由父在合适时机呈现，自身不渲染。
        if self._parent is not None:
            self._parent.notify(message)
        else:
            super().notify(message)

    @property
    def subagent_hub(self):
        # 委派到父传输的 hub，使任意层级的子 agent 共用顶层唯一的 Live 面板
        if self._parent is not None:
            return getattr(self._parent, "subagent_hub", None)
        return None

    def bind(self, stream) -> None:
        # 仅订阅事件流，渲染交给条目缓冲 + hub（不再创建任何独立 Live）
        super().bind(stream)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _est_lines(self, text: str) -> int:
        """粗略估计文本在面板内的渲染行数（按宽度折行），用于高度预算。"""
        w = max(10, self._console.size.width - 6)
        if not text:
            return 1
        total = 0
        for line in text.splitlines() or [""]:
            if not line:
                total += 1
                continue
            total += max(1, (len(line) + w - 1) // w)
        return total

    def _cap_lines(self, text: str, max_lines: int) -> str:
        lines = text.splitlines()
        if len(lines) <= max_lines:
            return text
        return "…(更早内容已省略)\n" + "\n".join(lines[-max_lines:])

    def _tool_call_panel(self, tc) -> "tuple[Any, int]":
        if tc.name == UPDATE_PLAN_TOOL_NAME:
            a = tc.arguments or {}
            sid = a.get("step_id", "?")
            st = a.get("status", "?")
            color = _PLAN_STATUS_COLOR.get(st, "white")
            note = a.get("note")
            text = f"[cyan]{sid}[/cyan] → [{color}]{st}[/{color}]" + (
                f"\n[dim]注: {note}[/dim]" if note else "")
            p = Panel(text, title="📋 计划更新", border_style="magenta", expand=False)
            return p, self._est_lines(text) + 2
        args = json.dumps(tc.arguments, ensure_ascii=False, indent=2)
        text = f"{tc.name}\n{args}"
        p = Panel(
            f"[cyan]{tc.name}[/cyan]\n```\n{args}\n```",
            title="🔧 工具调用", border_style="cyan", expand=False,
        )
        return p, self._est_lines(text) + 2

    def _tool_result_panel(self, tc, res) -> "tuple[Any, int]":
        if tc.name in (WRITE_TOOL_NAME, EDIT_TOOL_NAME) and res.ok and res.diff:
            diff = res.diff
            dcap = 4000
            if len(diff) > dcap:
                diff = diff[:dcap] + "\n…(diff 已截断)"
            p = Panel(
                Syntax(diff, "diff", theme="ansi_dark", word_wrap=True),
                title=f"✅ {tc.name} — {res.output}", border_style="green", expand=False,
            )
            return p, min(self._est_lines(diff) + 2, 200)
        style = "green" if res.ok else "red"
        body = res.output or res.error or ""
        if len(body) > 2000:
            body = body[:2000] + "\n…(已截断)"
        p = Panel(
            Markdown(body),
            title=f"[{'✅' if res.ok else '❌'}] {tc.name}",
            border_style=style, expand=False,
        )
        return p, self._est_lines(body) + 2

    def _finalize_stream(self) -> None:
        if self._stream_text:
            p = Panel(Markdown(self._stream_text), title="💬 模型输出",
                      border_style="green", expand=False)
            self._entries.append((p, self._est_lines(self._stream_text) + 2))
            self._stream_text = ""
        if self._reasoning_text:
            p = Panel("💭 " + self._reasoning_text, title="💭 思考",
                      border_style="dim", expand=False)
            self._entries.append((p, self._est_lines(self._reasoning_text) + 2))
            self._reasoning_text = ""

    def _refresh_sub(self) -> None:
        if self._slot is None or self.subagent_hub is None:
            return
        budget = self.subagent_hub.slot_budget()
        # 组装候选条目（已定稿 + 当前流式思考 + 当前流式输出）
        parts: list[tuple[Any, int]] = list(self._entries)
        if self._reasoning_text:
            rt = self._reasoning_text
            parts.append((Panel("💭 " + rt, title="💭 思考", border_style="dim", expand=False),
                          self._est_lines(rt) + 2))
        if self._stream_text:
            st = self._stream_text
            if self._est_lines(st) > budget:
                st = self._cap_lines(st, budget)
            parts.append((Panel(Markdown(st), title="💬 模型输出", border_style="green", expand=False),
                          min(self._est_lines(st) + 2, budget + 2)))
        # 预算裁剪：从最旧条目开始丢弃，直到总高 <= 预算（保证不撑出屏幕、不刷屏）
        total = sum(n for _, n in parts)
        while total > budget and len(parts) > 1:
            parts.pop(0)
            total = sum(n for _, n in parts)
        if not parts:
            render: Any = Panel("(等待子 agent 输出…)", title=self._slot.title,
                                border_style="blue", expand=False)
        else:
            render = Panel(Group(*[r for r, _ in parts]), title=self._slot.title,
                           border_style="blue", expand=False)
        self._slot.panel = render
        self.subagent_hub.refresh()

    def on_text(self, text: str, kind: str) -> None:
        if self._slot is not None:
            if kind == "reasoning":
                self._reasoning_text += text
            else:
                self._stream_text += text
            self._refresh_sub()
        elif self._use_live:
            # 交互但 hub 缺失（理论上极少）：退化为父级渲染
            return super().on_text(text, kind)
        else:
            # 后台非实时模式：仅累积，不在 ptk 提示期间渲染，避免与输入行争用终端
            if kind == "reasoning":
                self._reasoning_text += text
            else:
                self._stream_text += text

    def on_tool_call(self, tc) -> None:
        if self._slot is not None:
            self._finalize_stream()
            r, n = self._tool_call_panel(tc)
            self._entries.append((r, n))
            self._refresh_sub()
        elif self._use_live:
            return super().on_tool_call(tc)
        else:
            self._finalize_stream()
            r, n = self._tool_call_panel(tc)
            self._entries.append((r, n))

    def on_tool_call_delta(self, index, name, args_raw) -> None:
        if self._slot is not None:
            return  # 瞬时事件不单独渲染，避免面板抖动（定稿由 on_tool_call / on_tool_result 展示）
        elif self._use_live:
            return super().on_tool_call_delta(index, name, args_raw)
        # 后台非实时：忽略瞬时事件

    def on_tool_result(self, tc, res) -> None:
        if self._slot is not None:
            self._finalize_stream()
            if tc.name == SPAWN_SUBAGENT_TOOL_NAME:
                return
            if tc.name == UPDATE_PLAN_TOOL_NAME:
                return  # 已在 on_tool_call 以「📋 计划更新」面板展示，避免重复
            r, n = self._tool_result_panel(tc, res)
            self._entries.append((r, n))
            self._refresh_sub()
        elif self._use_live:
            return super().on_tool_result(tc, res)
        else:
            self._finalize_stream()
            if tc.name in (SPAWN_SUBAGENT_TOOL_NAME, UPDATE_PLAN_TOOL_NAME):
                return
            r, n = self._tool_result_panel(tc, res)
            self._entries.append((r, n))

    def on_plan_progress(self, ev) -> None:
        if self._slot is not None:
            self._finalize_stream()
            upd = ev.plan_update or {}
            text = f"{upd.get('step_id', '?')} → {upd.get('status', '?')}"
            p = Panel(text, title="📋 计划进度", border_style="magenta", expand=False)
            self._entries.append((p, self._est_lines(text) + 2))
            self._refresh_sub()
        elif self._use_live:
            return super().on_plan_progress(ev)
        else:
            self._finalize_stream()
            upd = ev.plan_update or {}
            text = f"{upd.get('step_id', '?')} → {upd.get('status', '?')}"
            p = Panel(text, title="📋 计划进度", border_style="magenta", expand=False)
            self._entries.append((p, self._est_lines(text) + 2))

    def _on_decision_done(self) -> None:
        if self._slot is not None:
            self._finalize_stream()
            self._refresh_sub()
        elif self._use_live:
            return super()._on_decision_done()
        else:
            self._finalize_stream()

    def report_usage(self, usage: dict[str, int] | None, answer: str | None = None) -> None:
        if self._slot is not None:
            return  # 子 agent 不单独刷 token 面板，避免与条目挤占高度
        elif self._use_live:
            return super().report_usage(usage, answer)
        # 后台非实时：不刷 token 面板

    def close(self) -> None:
        # 定稿并刷新后，把自身 slot 从 hub 注销（最后一个注销时 hub 自动 stop 其唯一 Live）
        if self._slot is not None:
            self._finalize_stream()
            self._refresh_sub()
            if self.subagent_hub is not None:
                self.subagent_hub.unregister(self._slot)
            self._slot = None
        else:
            # 后台非实时模式（live=False）：不在 ptk 输入行期间渲染，把累积条目定稿成最终面板，
            # 交给父传输在「不在流式 Live / 不在 ptk 输入行」的安全时机打印（见 queue_render /
            # flush_notifications）。
            if not self._use_live and self._parent is not None:
                self._finalize_stream()
                panel = self._build_final_panel()
                if panel is not None:
                    # 父传输未必是 TerminalTransport（协议未声明 queue_render）；用 getattr 容错，
                    # 无该方法时退化为直接打印（仅在非 ptk 争用环境下才有保障，生产父传输均支持）。
                    qr = getattr(self._parent, "queue_render", None)
                    if callable(qr):
                        qr(panel)
                    elif self._console is not None:
                        self._console.print(panel)
        super().close()

    def _build_final_panel(self):
        """把累积的已定稿条目 + 当前流式思考/输出 组装成最终面板（不刷 Live、不打印）。"""
        parts: list[tuple[Any, int]] = list(self._entries)
        if self._reasoning_text:
            rt = self._reasoning_text
            parts.append((Panel("💭 " + rt, title="💭 思考", border_style="dim", expand=False), 0))
        if self._stream_text:
            st = self._stream_text
            parts.append((Panel(Markdown(st), title="💬 模型输出", border_style="green", expand=False), 0))
        if not parts:
            return None
        return Panel(
            Group(*[r for r, _ in parts]),
            title=f"▶ subagent: {self._name}",
            border_style="blue",
            expand=False,
        )

    async def ask(self, question) -> str:
        if self._parent is not None and self._parent.interactive:
            return await self._parent.ask(question)
        raise RuntimeError("subagent 不应触发独立澄清交互（HITL 由父代理统一决策）")

    async def approve(self, action) -> bool:
        if self._parent is not None and self._parent.interactive:
            return await self._parent.approve(action)
        # 非交互（或 parent 为 None）：交给调用方配置的 gate 非交互默认放行
        return True


# --------------------------------------------------------------------------- #
# 交互式选项选择：单选用 prompt_toolkit 下拉箭头确认；多选用编号列表 + 自由输入
# （逗号分隔编号或标签），稳健不卡死。这些函数在交互式 TTY 下由 ``TerminalTransport.ask``
# 调用；非交互（run 无 TTY）不会进入。
# --------------------------------------------------------------------------- #
async def _ptk_single_choice(question: Question) -> str:
    """单选：prompt_toolkit 下拉箭头选择；任何异常/中断回退到自由输入。"""
    opts = question.options or []
    try:
        session = PromptSession(complete_while_typing=False)
        return await session.prompt_async(f"{question.question}\n> ", choices=opts)  # type: ignore[call-arg]
    except (EOFError, KeyboardInterrupt):
        return ""
    except Exception:
        return typer.prompt(question.question)


def _parse_multi_selection(line: str, opts: list[str]) -> list[str]:
    """把用户自由输入的「编号(逗号分隔) 或 标签(逗号分隔)」解析为已选选项（去重保序）。"""
    selected: list[str] = []
    for part in line.split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            idx = int(part)
            if 1 <= idx <= len(opts):
                selected.append(opts[idx - 1])
        elif part in opts:
            selected.append(part)
    seen: set[str] = set()
    return [s for s in selected if not (s in seen or seen.add(s))]


async def _ptk_multi_choice(question: Question) -> str:
    """多选：编号列表 + prompt_toolkit 自由输入（逗号分隔编号或标签）。

    不用 ``Application``+``CheckboxList``：后者在 rich 已占用 stdout 的 TTY 下会**不渲染
    选项且卡死**（现象：只看到「↑↓ 移动 · 空格勾选 · 回车确认」提示、无选项、回车无反应，
    且会把终端状态搞乱、残留空面板）。改为与单选下拉一致地复用 ``PromptSession`` 标准输入，
    永远不卡死；选项以编号列表显式打印，始终可见。
    """
    opts = question.options or []
    if not opts:
        return typer.prompt(question.question)
    for i, o in enumerate(opts, 1):
        Console().print(f"  [cyan]{i}[/cyan]. {o}")
    Console().print("[dim]可多选：输入编号(逗号分隔，如 1,3)或标签(逗号分隔)；直接回车=不选。[/dim]")
    session = PromptSession()
    try:
        line = await session.prompt_async("选择> ")
    except (EOFError, KeyboardInterrupt):
        return ""
    except Exception:
        return typer.prompt(question.question)
    return ", ".join(_parse_multi_selection(line, opts))
