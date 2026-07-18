"""CLI 入口（M1.6）：typer 应用，run / chat 子命令。

- ``run "<task>"``：一次性执行；``--plan`` 以 PLAN 模式起步（先产出计划、确认后才执行）；
  ``--no-clarify`` 关闭意图澄清；``--yes`` 跳过计划确认直接执行。
- ``chat``：交互式 REPL（多轮，单会话持续累积历史）。任意轮次可用命令自由切换模式：
  ``/plan`` 进入 PLAN 探索模式、``/exec`` 进入执行模式、``/approve`` 批准当前计划并切执行、
  ``/mode`` 查看当前模式；输入 ``exit``/``quit`` 退出。
- 渲染（rich）：流式输出 + 区分「思考 / 输出 / 工具调用」；最终答案用 Markdown 渲染；
  每轮 ReAct 循环结束打印 token 用量（usage）。
- 澄清（ask_clarification）：交互模式逐题收集答案回填；非交互（run 且无 TTY）报错退出。
- run 结束打印最简 trace（agent.run → model.act / tool.exec 父子树），完整可观测见 M5。

关键设计：plan/exec 模式是**会话级、按轮次可变**的状态，由 ``Session`` 持有并通过
``AgentLoop.run(plan_mode=, plan_path=)`` 传入。loop 本身无模式状态（构造期缺省仅作回落），
因此用户可在任意轮次切换，无需重建会话。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import typer
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.tree import Tree
from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.widgets import CheckboxList

from agent.config.settings import load_settings
from agent.core.intent import Question
from agent.core.model import create_model
from agent.core.presenter import LoopPresenter
from agent.core.session import Session
from agent.obs.tracer import Tracer
from agent.runtime.registry import default_registry

import agent.tools  # 导入即把 read/write/bash 登记到 default_registry（副作用）


# Windows 控制台/管道默认 GBK 编码，rich 输出 emoji（💭/💬/🔧 等）会抛 UnicodeEncodeError
# 导致整个命令崩溃；强制 stdout/stderr 走 UTF-8，保证中文与 emoji 正常（已是 UTF-8 的环境无影响）。
try:
    if sys.stdout.encoding and "utf-8" not in sys.stdout.encoding.lower():
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr.encoding and "utf-8" not in sys.stderr.encoding.lower():
        sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算：CJK 按 1 token/字，其余按 ~4 字符/token（无 tiktoken 依赖）。"""
    if not text:
        return 0
    cjk = sum(1 for c in text if ord(c) > 0x2E80)
    other = len(text) - cjk
    return cjk + other // 4 + 1


def _build_model(settings):
    """构建模型。测试可 monkeypatch 本函数注入 FakeModel。"""
    return create_model(settings)


# --------------------------------------------------------------------------- #
# 渲染层（rich）：流式实时输出 + 思考/输出/工具调用分区 + Markdown
# --------------------------------------------------------------------------- #
class _RichPresenter(LoopPresenter):
    """``LoopPresenter`` 的 rich 实现：把 ReAct 循环内部事件渲染成交互式终端输出。

    - 思考（reasoning）：暗色增量实时打印（``💭 思考:`` 头 + 逐片文本），不进框。
    - 输出（content）：用单个 ``Live`` 渲染**带框的 Markdown 面板**
      （``💬 模型输出``），流式过程中把面板裁剪到屏幕高度内（只显示最近内容），
      避免内容超高时整块重发刷屏；一个内容段结束（工具调用 / 整轮结束）时
      ``stop()`` 把**完整** Markdown 面板定稿打印一次。
    - 工具调用 / 结果：用 Panel 即时展示（清晰区分「工具调用」类别）。
    - ``report_usage`` 打印 token 用量。

    **为什么不重新引入历史刷屏 bug**：旧实现把所有轮次文本累积进同一个 ``_buf``
    且跨模型轮次不清空，导致 ``Live`` 每帧渲染的面板越来越长、最终超过屏幕，
    每次 ``refresh()`` 整块重发 → 十几份相同面板。本实现：① 每个内容段用独立的
    ``_buf``，``Live`` 实例在段开始时创建、段结束时 ``stop()`` 后丢弃，绝不跨段
    累积；② 流式时用 ``_render_content(cap=True)`` 把面板高度裁到屏幕内，就地刷新
    （同高 → 不滚动、不重发）；③ 段结束才 ``stop()`` 渲染一次完整面板（仅一次滚动）。
    """

    def __init__(self) -> None:
        self._console = Console()
        self._saw_reasoning = False   # 本思考段是否已打印过 "💭 思考:" 头
        self._live = None             # 当前内容段的 Live（流式 Markdown 面板）
        self._buf = ""                # 当前内容段累积文本

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

    def on_tool_call(self, tc) -> None:
        self._commit_live()
        self._end_reasoning_segment()
        self._console.print()  # 与上方模型输出分隔
        args = json.dumps(tc.arguments, ensure_ascii=False, indent=2)
        self._console.print(
            Panel(
                f"[cyan]{tc.name}[/cyan]\n```\n{args}\n```",
                title="🔧 工具调用",
                border_style="cyan",
                expand=False,
            )
        )

    def on_tool_result(self, tc, res) -> None:
        self._commit_live()
        self._end_reasoning_segment()
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

    def close(self) -> None:
        # 收尾任何未闭合的流式内容段（定稿为完整 Markdown 面板）
        self._commit_live()
        self._end_reasoning_segment()

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


# --------------------------------------------------------------------------- #
# 会话交互层（typer）：澄清提问 / 计划确认 / 提示
# --------------------------------------------------------------------------- #
# 交互式选项选择：prompt_toolkit 提供「上下箭头移动 + 回车确认」（多选用空格勾选）。
# 这些函数在交互式 TTY 下由 ``_TyperUI.ask`` 调用；非交互（run 无 TTY）不会进入。
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


async def _ptk_multi_choice(question: Question) -> str:
    """多选：prompt_toolkit CheckboxList（↑↓ 移动、空格勾选、回车确认）。"""
    opts = question.options or []
    cb = CheckboxList(values=[(o, o) for o in opts])
    kb = KeyBindings()

    @kb.add("c-c")
    def _(event):
        event.app.exit()

    app = Application(layout=Layout(cb), key_bindings=kb, full_screen=False)
    try:
        await app.run_async()
    except (EOFError, KeyboardInterrupt):
        return ""
    except Exception:
        return typer.prompt(question.question)
    return ", ".join(cb.current_values)


class _TyperUI:
    """``SessionUI`` 的 typer 实现：把会话编排所需的人机交互落到终端。

    澄清提问：有选项时用 prompt_toolkit 箭头选择（单选下拉 / 多选 CheckboxList），
    无选项时回退 ``typer.prompt`` 自由输入；计划展示走 rich Markdown（其余提示走 typer.echo）。
    """

    def __init__(self, *, interactive: bool):
        self._interactive = interactive
        self._console = Console()

    @property
    def interactive(self) -> bool:
        return self._interactive

    async def ask(self, question: Question) -> str:
        # 先空一行：模型的流式思考/输出以 end="" 收尾、无换行，若不分隔会与澄清面板同行粘连。
        self._console.print()
        if question.options:
            self._console.print(
                Panel(question.question, title="❓ 澄清", border_style="yellow", expand=False)
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
        for s in res.plan_steps or []:
            self._console.print(f"  [{s.status}] {s.id}: {s.title}")
        self._console.print(f"(plan file: {res.plan_path})")

    def confirm_plan(self) -> bool:
        return typer.confirm("是否执行该计划？", default=False)

    def notify(self, message: str) -> None:
        typer.echo(message, err=True)


def _print_trace(tracer: Tracer) -> None:
    """用 rich Tree 美化 trace（保留父子关系），模型调用节点展示 total token。"""
    tree = Tree("[bold cyan]agent trace[/bold cyan]")
    nodes: dict[str | None, Tree] = {None: tree}
    # 按开始时间排序，保证父子/兄弟顺序稳定
    for s in sorted(tracer.spans, key=lambda x: x.started_at):
        parent = nodes.get(s.parent_id, tree)
        dur_ms = (s.ended_at or time.time()) - s.started_at
        label = (
            f"[bold]{s.name}[/bold] [dim]({s.kind} · {dur_ms * 1000:.1f}ms · id={s.id})[/dim]"
        )
        # 模型调用 span：仅展示 total token
        if "usage" in s.meta:
            label += f" [green]· total={s.meta['usage'].get('total_tokens', 0)} tok[/green]"
        nodes[s.id] = parent.add(label)
    console = Console()
    console.print(Panel(tree, title="🔍 Trace", border_style="blue", expand=False))


app = typer.Typer(help="通用编码 Agent（类 Claude Code / Codex）", add_completion=False)


@app.command()
def run(
    task: str,
    plan: bool = typer.Option(False, "--plan", "--no-plan", help="以 PLAN 模式起步：先产出计划、确认后再执行"),
    yes: bool = typer.Option(False, "--yes", help="跳过计划确认，直接进入执行"),
    no_clarify: bool = typer.Option(False, "--no-clarify", help="关闭意图澄清"),
) -> None:
    settings = load_settings(clarify_enabled=not no_clarify, plan_mode=plan)
    model = _build_model(settings)
    tracer = Tracer()
    reg = default_registry
    session = Session(model, reg, settings, tracer, plan_mode=plan)

    ui = _TyperUI(interactive=sys.stdin.isatty())
    presenter = _RichPresenter()
    try:
        res, err = asyncio.run(
            session.step(task, ui, yes=yes, fatal_plan_decline=True, presenter=presenter)
        )
    except Exception as e:  # 任何未捕获异常（含 LoopStalled / 真实 API 错误）都优雅退出
        typer.echo(f"error: {type(e).__name__}: {e}", err=True)
        err = 1
        res = None

    # 一轮 ReAct 循环结束：停止 Live（保留最终答案），打印 token 用量
    presenter.close()
    if res is not None:
        presenter.report_usage(res.usage, res.text)
        # 最终答案已通过流式 Live 实时渲染，无需重复打印 res.text
    typer.echo("")
    _print_trace(tracer)
    raise typer.Exit(code=err if err is not None else 0)


@app.command()
def chat() -> None:
    """交互式 REPL：多轮对话，单一会话持续累积历史。

    任意轮次用命令切换模式：/plan（探索） / exec（执行） / approve（批准计划并切执行）
    / mode（查看当前模式）。输入 exit/quit 退出。
    """
    settings = load_settings()
    try:
        model = _build_model(settings)
    except Exception as e:  # 配置错误（如缺 API key）优雅退出，不吐底层栈
        typer.echo(f"error: {type(e).__name__}: {e}", err=True)
        raise typer.Exit(code=1)
    tracer = Tracer()
    reg = default_registry
    session = Session(model, reg, settings, tracer, plan_mode=settings.plan_mode)
    ui = _TyperUI(interactive=True)

    typer.echo("进入 chat 模式（/plan /exec 切换模式；exit/quit 退出）。")
    while True:
        try:
            task = typer.prompt("you")
        except (EOFError, KeyboardInterrupt):
            break
        cmd = task.strip().lower()
        if cmd in {"exit", "quit"}:
            break
        if cmd in {"/plan"}:
            session.plan_mode = True
            typer.echo("→ 已切换到 PLAN 模式（探索，不修改任何文件）", err=True)
            continue
        if cmd in {"/exec"}:
            session.plan_mode = False
            typer.echo("→ 已切换到 EXEC 模式（可执行）", err=True)
            continue
        if cmd in {"/approve"}:
            if session.plan_path:
                session.plan_mode = False
                typer.echo(f"→ 已批准计划并切到 EXEC 模式：{session.plan_path}", err=True)
            else:
                typer.echo("→ 当前没有待批准的计划（先用 /plan 让模型产出计划）", err=True)
            continue
        if cmd in {"/mode"}:
            typer.echo(f"→ 当前模式：{'PLAN' if session.plan_mode else 'EXEC'}"
                       + (f"，计划：{session.plan_path}" if session.plan_path else ""), err=True)
            continue

        presenter = _RichPresenter()
        try:
            res, err = asyncio.run(
                session.step(task, ui, yes=False, fatal_plan_decline=False, presenter=presenter)
            )
        except Exception as e:  # 任何未捕获异常（真实 API 错误等）优雅退出
            presenter.close()
            typer.echo(f"error: {type(e).__name__}: {e}", err=True)
            err = 1
            res = None
        else:
            presenter.close()
        if res is not None:
            presenter.report_usage(res.usage, res.text)
            # 最终答案已通过流式 Live 实时渲染，无需重复打印 res.text
        if err == 2:
            typer.echo("（需要交互澄清但环境非交互，已退出）", err=True)
            break

    typer.echo("")
    _print_trace(tracer)


if __name__ == "__main__":
    app()
