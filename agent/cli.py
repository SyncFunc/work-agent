"""CLI 入口（M1.6）：typer 应用，run / chat 子命令。

M3.1 增强：
- ``run`` / ``chat`` 自动持久化 trace 到 SQLite。
- ``--no-trace`` 关闭 trace（不创建 tracer / trace_store）。
- 退出前持久化最终 trace。
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
from agent.core.model import Message, create_model
from agent.core.session import Session
from agent.obs.tracer import Tracer
from agent.obs.store import TraceStore
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


def _build_model(settings, tracer=None, pipeline=None):
    """构建模型。测试可 monkeypatch 本函数注入 FakeModel。"""
    return create_model(settings, tracer=tracer, pipeline=pipeline)


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
    if tracer is None:
        return
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
        # 展示最近的 warn/error log
        recent_logs = [lg for lg in s.logs if lg.level in ("warn", "error")][-2:]
        for lg in recent_logs:
            color = "yellow" if lg.level == "warn" else "red"
            label += f" [{color}]⚠ {lg.key}[/{color}]"
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
    no_trace: bool = typer.Option(False, "--no-trace", help="关闭 trace 记录"),
) -> None:
    settings = load_settings(clarify_enabled=not no_clarify, plan_mode=plan)
    tracer = None if no_trace else Tracer()
    model = _build_model(settings, tracer=tracer)
    trace_store = None if no_trace else TraceStore(settings.obs.db_path)
    reg = default_registry
    session = Session(model, reg, settings, tracer, plan_mode=plan, trace_store=trace_store)

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
    # 退出前持久化最终 trace
    if tracer is not None and trace_store is not None:
        trace_store.save_trace(tracer)
    _print_trace(tracer)
    raise typer.Exit(code=err if err is not None else 0)


@app.command()
def chat() -> None:
    """交互式 REPL：多轮对话，单一会话持续累积历史。

    任意轮次用命令切换模式：/plan（探索） / exec（执行） / approve（批准计划并切执行）
    / mode（查看当前模式）。扩展命令：/skills（列出 skill）、/agents（列出 subagent 类型）、
    /skill <name>（显式加载某 skill 到下一轮）。输入 exit/quit 退出。
    """
    settings = load_settings()
    try:
        tracer = Tracer() if settings.obs.enabled else None
        from agent.resilience.pipeline import build_llm_pipeline
        llm_pipeline = build_llm_pipeline(settings)
        model = _build_model(settings, tracer=tracer, pipeline=llm_pipeline)
    except Exception as e:  # 配置错误（如缺 API key）优雅退出，不吐底层栈
        typer.echo(f"error: {type(e).__name__}: {e}", err=True)
        raise typer.Exit(code=1)
    trace_store = TraceStore(settings.obs.db_path) if settings.obs.enabled else None
    reg = default_registry
    session = Session(model, reg, settings, tracer, plan_mode=settings.plan.mode, trace_store=trace_store)
    transport = TerminalTransport(interactive=True)

    typer.echo("进入 chat 模式（/plan /exec 切换模式；/skills /agents 查看扩展；exit/quit 退出）。")
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
            if session.plan_path is None and os.path.isfile(settings.plan.file):
                session.plan_path = settings.plan.file
            typer.echo("→ 已切换到 EXEC 模式（可执行）", err=True)
            continue
        if cmd in {"/approve"}:
            # 同上：自动载人已落盘计划，避免「已展示未批准」状态下丢失 plan_path。
            if session.plan_path is None and os.path.isfile(settings.plan.file):
                session.plan_path = settings.plan.file
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
        # ---- M5.4：Skill / Subagent 命令（仅改 Session 状态，不调模型）----
        if cmd in {"/skills"}:
            transport.show_skills(session.list_skills())
            continue
        if cmd in {"/agents"}:
            transport.show_agents(session.list_agents())
            continue
        if cmd in {"/skill"}:
            typer.echo("用法: /skill <name>  —— 显式加载某 skill 到下一轮对话", err=True)
            continue
        if cmd.startswith("/skill "):
            name = task.strip()[len("/skill "):].strip()  # 保留原名大小写
            if session.skill_loader is None:
                typer.echo("skills 未启用（settings.skills.enabled=false）", err=True)
            else:
                spec = session.skill_loader.get(name)
                if spec is None:
                    typer.echo(f"未找到 skill: {name}", err=True)
                else:
                    # 等价于模型调 use_skill：把渲染后的正文作为 user 消息注入下一轮
                    session.messages.append(Message(
                        role="user", content=f"[Skill {name}]\n{spec.render_body()}"
                    ))
                    typer.echo(f"已加载 skill: {name}", err=True)
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
    # 退出前持久化最终 trace
    if tracer is not None and trace_store is not None:
        trace_store.save_trace(tracer)
    _print_trace(tracer)


@app.command()
def health(
    watch: bool = typer.Option(False, "--watch", "-w", help="持续轮询（每 5 秒刷新）"),
    port: int = typer.Option(0, "--port", "-p", help="启动 HTTP 健康端点（端口号，如 9090）"),
) -> None:
    """检查 Agent 各组件健康状态。

    不带参数：一次性检查并输出。
    --watch：持续轮询，Live 实时刷新。
    --port 9090：启动 HTTP /health 端点。
    """
    import asyncio
    from http.server import HTTPServer

    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table

    from agent.config.settings import load_settings as _ls
    import agent.resilience.health as _health_mod

    settings = _ls()
    checker = _health_mod.build_default_health_checks(settings)
    console = Console()

    # HTTP 端点启动（可选）
    http_server: HTTPServer | None = None
    if port:
        _health_mod._HTTP_CHECKER = checker  # 注入到模块级变量供 handler 使用
        http_server = HTTPServer(("127.0.0.1", port), _health_mod.HealthHTTPHandler)
        typer.echo(f"HTTP 健康端点已启动：http://127.0.0.1:{port}/health", err=True)

    def _render_health(status) -> Panel:
        table = Table(show_header=True, header_style="bold")
        table.add_column("组件", style="cyan")
        table.add_column("状态", width=10)
        table.add_column("详情")
        for name, cr in status.checks.items():
            color = {"ok": "green", "degraded": "yellow", "fail": "red"}.get(cr.status, "white")
            marker = {"ok": "✅", "degraded": "⚠️", "fail": "❌"}.get(cr.status, "?")
            table.add_row(name, f"[{color}]{marker} {cr.status}[/{color}]", cr.detail)
        title = "✅ 全部正常" if status.healthy else "⚠️ 存在异常"
        border = "green" if status.healthy else "yellow"
        return Panel(table, title=f"🔍 健康检查 — {title}", border_style=border, expand=False)

    if watch:
        with Live(_render_health(_health_mod.HealthStatus(healthy=True)), console=console, refresh_per_second=0.2, auto_refresh=False) as live:
            while True:
                status = asyncio.run(checker.check_all())
                live.update(_render_health(status))
                live.refresh()
                if http_server:
                    http_server.handle_request()
                asyncio.run(asyncio.sleep(5))
    else:
        status = asyncio.run(checker.check_all())
        console.print(_render_health(status))
        has_fail = any(cr.status == "fail" for cr in status.checks.values())
        has_degraded = any(cr.status == "degraded" for cr in status.checks.values())
        if has_fail:
            raise typer.Exit(code=2)
        if has_degraded:
            raise typer.Exit(code=1)
        raise typer.Exit(code=0)


if __name__ == "__main__":
    app()
