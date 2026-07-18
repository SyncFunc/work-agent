"""CLI 入口（M1.6）：typer 应用，run / chat 子命令。

- ``run "<task>"``：一次性执行；``--plan`` 以 PLAN 模式起步（先产出计划、确认后才执行）；
  ``--no-clarify`` 关闭意图澄清；``--yes`` 跳过计划确认直接执行。
- ``chat``：交互式 REPL（多轮，单会话持续累积历史）。任意轮次可用命令自由切换模式：
  ``/plan`` 进入 PLAN 探索模式、``/exec`` 进入执行模式、``/approve`` 批准当前计划并切执行、
  ``/mode`` 查看当前模式；输入 ``exit``/``quit`` 退出。
- 渲染（rich）与 HITL 交互统一由 ``TerminalTransport`` 实现，见
  ``agent.runtime.terminal_transport``：``Session.step`` 接收 transport，loop 只往事件流
  落事件，终端呈现完全订阅事件驱动，本文件只负责命令编排与 trace 打印。
- 澄清（ask_clarification）：交互模式逐题收集答案回填；非交互（run 且无 TTY）报错退出。
- run 结束打印最简 trace（agent.run → model.act / tool.exec 父子树），完整可观测见 M5。

关键设计：plan/exec 模式是**会话级、按轮次可变**的状态，由 ``Session`` 持有并通过
``AgentLoop.run(plan_mode=, plan_path=)`` 传入。loop 本身无模式状态（构造期缺省仅作回落），
因此用户可在任意轮次切换，无需重建会话。
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import time
from typing import cast

import typer
from rich.console import Console
from rich.panel import Panel
from rich.tree import Tree


from agent.config.settings import load_settings
from agent.core.model import create_model
from agent.core.session import Session
from agent.obs.tracer import Tracer
from agent.runtime.registry import default_registry
from agent.runtime.terminal_transport import TerminalTransport

import agent.tools  # 导入即把 read/write/bash 登记到 default_registry（副作用）

_ = agent.tools  # 显式引用，保留副作用导入，避免未使用告警


# Windows 控制台/管道默认 GBK 编码，rich 输出 emoji（💭/💬/🔧 等）会抛 UnicodeEncodeError
# 导致整个命令崩溃；强制 stdout/stderr 走 UTF-8，保证中文与 emoji 正常（已是 UTF-8 的环境无影响）。
try:
    if sys.stdout.encoding and "utf-8" not in sys.stdout.encoding.lower():
        cast(io.TextIOWrapper, sys.stdout).reconfigure(encoding="utf-8")
    if sys.stderr.encoding and "utf-8" not in sys.stderr.encoding.lower():
        cast(io.TextIOWrapper, sys.stderr).reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def _build_model(settings):
    """构建模型。测试可 monkeypatch 本函数注入 FakeModel。"""
    return create_model(settings)


def _render_soft_limit(res) -> None:
    """max_iterations 软上限命中提示：不中断会话，仅告知用户上下文已保留、可接棒续跑。"""
    if res is None:
        return
    if getattr(res, "soft_limit_hit", False):
        Console().print(
            Panel(res.text, title="⚠️ 轮次上限", border_style="yellow", expand=False)
        )


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

    transport = TerminalTransport(interactive=sys.stdin.isatty())
    try:
        res, err = asyncio.run(
            session.step(task, transport, yes=yes, fatal_plan_decline=True)
        )
    except Exception as e:  # 任何未捕获异常（含 LoopStalled / 真实 API 错误）都优雅退出
        typer.echo(f"error: {type(e).__name__}: {e}", err=True)
        err = 1
        res = None

    # 一轮 ReAct 循环结束：停止 Live（保留最终答案），打印 token 用量
    transport.close()
    if res is not None:
        transport.report_usage(res.usage, res.text)
        _render_soft_limit(res)
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
    transport = TerminalTransport(interactive=True)

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
            # 若尚无已知计划但计划文件已落盘（如刚 /plan 产出未显式批准），自动载人，
            # 使 EXEC 模式能按 plan_path 下发 update_plan（推进步骤进度）。
            if session.plan_path is None and os.path.isfile(settings.plan_file):
                session.plan_path = settings.plan_file
            typer.echo("→ 已切换到 EXEC 模式（可执行）", err=True)
            continue
        if cmd in {"/approve"}:
            # 同上：自动载人已落盘计划，避免「已展示未批准」状态下丢失 plan_path。
            if session.plan_path is None and os.path.isfile(settings.plan_file):
                session.plan_path = settings.plan_file
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

        try:
            res, err = asyncio.run(
                session.step(task, transport, yes=False, fatal_plan_decline=False)
            )
        except Exception as e:  # 任何未捕获异常（真实 API 错误等）优雅退出
            transport.close()
            typer.echo(f"error: {type(e).__name__}: {e}", err=True)
            err = 1
            res = None
        else:
            transport.close()
        if res is not None:
            transport.report_usage(res.usage, res.text)
            _render_soft_limit(res)
            # 最终答案已通过流式 Live 实时渲染，无需重复打印 res.text
        if err == 2:
            typer.echo("（需要交互澄清但环境非交互，已退出）", err=True)
            break

    typer.echo("")
    _print_trace(tracer)


if __name__ == "__main__":
    app()
