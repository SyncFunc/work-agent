"""CLI 入口（M1.6）：typer 应用，run / chat 子命令。

M3.1 增强：
- ``run`` / ``chat`` 自动持久化 trace 到 SQLite。
- ``--no-trace`` 关闭 trace（不创建 tracer / trace_store）。
- 退出前持久化最终 trace。
"""

from __future__ import annotations

import asyncio
import io
import re
import sys
import time
import uuid
from typing import cast

import typer
from prompt_toolkit.formatted_text import FormattedText
from rich.console import Console
from rich.panel import Panel
from rich.tree import Tree

import agent.tools  # 导入即把 read/write/bash 登记到 default_registry（副作用）
from agent.config.settings import load_settings
from agent.context.session_store import SessionStore
from agent.core.model import create_model
from agent.core.session import Session
from agent.core.session_command import dispatch_command
from agent.obs.store import TraceStore
from agent.obs.tracer import Tracer
from agent.runtime.registry import default_registry
from agent.runtime.terminal_transport import TerminalTransport

_ = agent.tools  # 显式引用，保留副作用导入，避免未使用告警

# 状态栏渲染辅助：rich 标记经 prompt_toolkit 时不能直接渲染，需剥离或转 FormattedText。
_RICH_TAG = re.compile(r"\[/?[a-zA-Z_]+\]")
# 解析 _status_line() 输出：ctx: [green]10%[/green]
_STATUS_RE = re.compile(r"ctx:\s*\[(\w+)\]([\d.]+%)\[/\1\]")
_STATUS_COLOR = {"green": "ansigreen", "yellow": "ansiyellow", "red": "ansired"}


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


def _ensure_scaffold() -> None:
    """首次运行：在项目级 .agent/ 下自动生成配置骨架（已存在则跳过，不覆盖）。"""
    from agent.config.settings import scaffold_project

    created = scaffold_project()
    made = [k for k, v in created.items() if v]
    if made:
        typer.echo(f"[init] 已为项目创建配置骨架 .agent/：{', '.join(made)}", err=True)


def _render_soft_limit(res) -> None:
    """max_iterations 软上限命中提示：不中断会话，仅告知用户上下文已保留、可接棒续跑。"""
    if res is None:
        return
    if getattr(res, "soft_limit_hit", False):
        Console().print(Panel(res.text, title="⚠️ 轮次上限", border_style="yellow", expand=False))


def _print_trace(tracer: Tracer | None) -> None:
    """用 rich Tree 美化 trace（保留父子关系），模型调用节点展示 total token。"""
    if tracer is None:
        return
    tree = Tree("[bold cyan]agent trace[/bold cyan]")
    nodes: dict[str | None, Tree] = {None: tree}
    # 按开始时间排序，保证父子/兄弟顺序稳定
    for s in sorted(tracer.spans, key=lambda x: x.started_at):
        parent = nodes.get(s.parent_id, tree)
        dur_ms = (s.ended_at or time.time()) - s.started_at
        label = f"[bold]{s.name}[/bold] [dim]({s.kind} · {dur_ms * 1000:.1f}ms · id={s.id})[/dim]"
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
    plan: bool = typer.Option(
        False, "--plan", "--no-plan", help="以 PLAN 模式起步：先产出计划、确认后再执行"
    ),
    yes: bool = typer.Option(False, "--yes", help="跳过计划确认，直接进入执行"),
    no_clarify: bool = typer.Option(False, "--no-clarify", help="关闭意图澄清"),
    no_trace: bool = typer.Option(False, "--no-trace", help="关闭 trace 记录"),
) -> None:
    _ensure_scaffold()
    settings = load_settings(clarify_enabled=not no_clarify, plan_mode=plan)
    tracer = None if no_trace else Tracer()
    model = _build_model(settings, tracer=tracer)
    trace_store = None if no_trace else TraceStore(settings.obs.db_path)
    reg = default_registry
    session_id = uuid.uuid4().hex
    session_store = SessionStore(settings.obs.sessions_db_path)
    session_store.create(session_id)
    session = Session(
        model,
        reg,
        settings,
        tracer,
        plan_mode=plan,
        trace_store=trace_store,
        session_id=session_id,
        session_store=session_store,
    )

    transport = TerminalTransport(interactive=sys.stdin.isatty(), context_mgr=session.context_mgr)
    try:
        res, err = asyncio.run(session.step(task, transport, yes=yes, fatal_plan_decline=True))
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
    /skill <name>（显式加载某 skill 到下一轮）、/context（查看上下文占用）、/compact（手动压缩）、
    /resume <id>（切换到已持久化的会话）、/fork <id>（从某会话派生新分支）。
    输入 exit/quit 退出。
    """
    _ensure_scaffold()
    settings = load_settings()
    try:
        tracer = Tracer() if settings.obs.enabled else None
        from agent.resilience.pipeline import build_llm_pipeline

        llm_pipeline = build_llm_pipeline(settings)
        model = _build_model(settings, tracer=tracer, pipeline=llm_pipeline)
    except Exception as e:  # 配置错误（如缺 API key）优雅退出，不吐底层栈
        typer.echo(f"error: {type(e).__name__}: {e}", err=True)
        raise typer.Exit(code=1) from None
    trace_store = TraceStore(settings.obs.db_path) if settings.obs.enabled else None
    reg = default_registry
    session_id = uuid.uuid4().hex
    session_store = SessionStore(settings.obs.sessions_db_path)
    session_store.create(session_id)
    session = Session(
        model,
        reg,
        settings,
        tracer,
        plan_mode=settings.plan.mode,
        trace_store=trace_store,
        session_id=session_id,
        session_store=session_store,
    )
    transport = TerminalTransport(interactive=True, context_mgr=session.context_mgr)

    typer.echo(
        "进入 chat 模式（/plan /exec 切换模式；/skills /agents 查看扩展；/agent <name> <task> 后台运行；/bg 查看后台任务；/context 查看占用；/compact 手动压缩；/resume <id> 恢复会话；/fork <id> 派生分支；exit/quit 退出）。"
    )
    try:
        asyncio.run(_chat_repl(session, transport, settings, session_store=session_store))
    except KeyboardInterrupt:
        pass
    typer.echo("")
    # 退出前持久化最终 trace
    if tracer is not None and trace_store is not None:
        trace_store.save_trace(tracer)
    _print_trace(tracer)


def _build_session_from_store(settings, store, session_id: str) -> Session:
    """从 SessionStore 恢复一个已持久化会话（M6.2）。不存在则报错退出。"""
    if store.get_session(session_id) is None:
        typer.echo(f"未找到会话: {session_id}", err=True)
        raise typer.Exit(1)
    tracer = Tracer() if settings.obs.enabled else None
    model = _build_model(settings, tracer=tracer)
    trace_store = TraceStore(settings.obs.db_path) if settings.obs.enabled else None
    return Session.from_store(
        model, default_registry, settings, store, session_id, tracer=tracer, trace_store=trace_store
    )


def _fork_session_from_store(settings, store, session_id: str, name: str | None = None) -> Session:
    """从父会话 fork 出新分支并恢复（M6.2）。"""
    if store.get_session(session_id) is None:
        typer.echo(f"未找到会话: {session_id}", err=True)
        raise typer.Exit(1)
    new_id = store.fork(session_id, name=name)
    typer.echo(f"已 fork 新会话 {new_id}（父: {session_id}）", err=True)
    return _build_session_from_store(settings, store, new_id)


@app.command()
def resume(
    session_id: str = typer.Argument(..., help="要恢复的会话 id"),
) -> None:
    """恢复一个已持久化的会话并进入 chat（M6.2）。"""
    _ensure_scaffold()
    settings = load_settings()
    store = SessionStore(settings.obs.sessions_db_path)
    session = _build_session_from_store(settings, store, session_id)
    transport = TerminalTransport(interactive=True, context_mgr=session.context_mgr)
    typer.echo(f"已恢复会话 {session_id}（{len(session.messages)} 条消息）")
    try:
        asyncio.run(_chat_repl(session, transport, settings, session_store=store))
    except KeyboardInterrupt:
        pass
    _print_trace(None)


@app.command()
def fork(
    session_id: str = typer.Argument(..., help="要 fork 的父会话 id"),
    name: str = typer.Option(None, "--name", "-n", help="分支名"),
) -> None:
    """从某会话派生新分支并进入 chat（M6.2）。"""
    _ensure_scaffold()
    settings = load_settings()
    store = SessionStore(settings.obs.sessions_db_path)
    session = _fork_session_from_store(settings, store, session_id, name=name)
    transport = TerminalTransport(interactive=True, context_mgr=session.context_mgr)
    try:
        asyncio.run(_chat_repl(session, transport, settings, session_store=store))
    except KeyboardInterrupt:
        pass
    _print_trace(None)


async def _chat_repl(
    session: Session, transport: TerminalTransport, settings, session_store=None
) -> None:
    """异步 REPL 主循环：单一事件循环驱动前台 step 与后台 Subagent 并发。

    - 交互 TTY：用 prompt_toolkit 的 ``prompt_async`` 等待输入，等待期间事件循环仍
      可调度后台 Subagent（``spawn_background`` 用 ``asyncio.create_task`` 挂到本 loop），
      因此后台任务在用户思考/输入时持续推进，真正「后台」运行。
    - 非交互（管道/CliRunner）：退化为同步 ``typer.prompt``（此时后台在等待输入期间不
      运行，但在前台 step 的 await 期间仍会推进）。
    - 后台 Subagent 的完成/错误通知由其自身 ``transport.notify`` 负责（渲染是 transport 层
      的职责，Session 不持有任何渲染逻辑）。
    """
    from prompt_toolkit import PromptSession

    # 仅在「真正 TTY」时用 prompt_async：等待输入期间事件循环仍能调度后台 Subagent。
    # 非 TTY（管道 / CliRunner）退化为同步 typer.prompt，否则 prompt_toolkit 在
    # 无终端环境下会卡死。
    ptk = PromptSession() if (transport.interactive and sys.stdin.isatty()) else None
    while True:
        # 每轮等待输入前刷出积压通知（后台 Subagent 完成/错误可能在空闲时到达）；
        # 通知由 TerminalTransport 在不在流式 Live 中的安全时机呈现，不会打断前台渲染。
        transport.flush_notifications()
        # M4.6 修复：状态栏含 rich 标记，但 prompt_toolkit 的 prompt 不直接渲染 rich markup，
        # 会原样打印 ``[green]`` 等标签。交互模式改为 prompt_toolkit 的 FormattedText（着色），
        # 非交互模式（CliRunner/管道）剥离标记仅显示纯文本。
        status = transport._status_line()
        try:
            if ptk is not None:
                if status:
                    m = _STATUS_RE.match(status)
                    col = m.group(1) if m else "green"
                    pct = m.group(2) if m else status
                    ft_color = _STATUS_COLOR.get(col, "ansigreen")
                    ft = FormattedText([(ft_color, f"ctx: {pct} "), ("", "| you: ")])
                else:
                    ft = FormattedText([("", "you")])
                task = await ptk.prompt_async(message=ft)
            else:
                status_plain = _RICH_TAG.sub("", status)
                prefix = f"{status_plain} | you" if status_plain else "you"
                task = typer.prompt(prefix)
        except (EOFError, KeyboardInterrupt):
            break
        cmd = task.strip().lower()
        if cmd in {"exit", "quit"}:
            break
        # M6.2：会话切换命令需在 REPL 内 rebind 局部 session 变量（dispatch_command
        # 无法改调用方的 session 引用，故在此前置拦截）。
        if cmd.startswith("/resume "):
            sid = task.strip()[len("/resume ") :].strip()
            session = _build_session_from_store(settings, session_store, sid)
            transport._context_mgr = session.context_mgr
            typer.echo(f"已恢复会话 {sid}（{len(session.messages)} 条消息）", err=True)
            continue
        if cmd.startswith("/fork "):
            sid = task.strip()[len("/fork ") :].strip()
            session = _fork_session_from_store(settings, session_store, sid)
            transport._context_mgr = session.context_mgr
            typer.echo(f"已进入 fork 分支（{len(session.messages)} 条消息）", err=True)
            continue
        # M7.5：命令分发抽为共享函数（进程内与 daemon 协议路径共用，单一来源）。
        if await dispatch_command(session, task, transport, settings):
            continue

        try:
            res, err = await session.step(task, transport, yes=False, fatal_plan_decline=False)
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

    # 退出前：若有后台 Subagent 仍在运行，等待其正常收尾（最多 30s），避免被 asyncio.run 粗暴取消
    # 导致正在执行的工具中断、文件/状态不一致（见 Session.shutdown_background）。
    running = session.list_background_tasks()
    if running:
        typer.echo(f"⏳ 正在等待 {len(running)} 个后台 Subagent 完成（最多 30s）...", err=True)
        cancelled = await session.shutdown_background(timeout=30.0)
        if cancelled:
            typer.echo(f"⚠️ 以下后台 Subagent 超时未结束，已取消：{', '.join(cancelled)}", err=True)
    # 退出前再刷一次，避免最后一轮命令产生的通知丢失
    transport.flush_notifications()


@app.command()
def init() -> None:
    """初始化项目配置骨架：生成 .agent/settings.yaml、skills/、agents/、AGENTS.md。

    仅创建缺失的文件/目录，绝不覆盖已存在的 settings.yaml（以免破坏你的配置）。
    首次运行 run/chat 也会自动执行同样的逻辑（已存在则跳过）。
    """
    from agent.config.settings import scaffold_project

    created = scaffold_project(create_if_exists=True)
    made = [k for k, v in created.items() if v]
    if made:
        typer.echo(f"已创建配置骨架：{', '.join(made)}", err=True)
    else:
        typer.echo("配置骨架已存在，无需创建（.agent/settings.yaml 不会被覆盖）。", err=True)


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

    import agent.resilience.health as _health_mod
    from agent.config.settings import load_settings as _ls

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
        with Live(
            _render_health(_health_mod.HealthStatus(healthy=True)),
            console=console,
            refresh_per_second=0.2,
            auto_refresh=False,
        ) as live:
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


@app.command()
def daemon(
    port: int = typer.Option(
        None, "--port", "-p", help="daemon 监听端口（覆盖 settings.daemon.port）"
    ),
) -> None:
    """启动 agentrunner 守护进程（常驻），前端经 WebSocket 连接；仅绑 127.0.0.1。"""
    from agent.daemon.server import start_daemon

    settings = load_settings()
    if port:
        settings.daemon.port = port
    start_daemon(settings)


@app.command()
def client(
    port: int = typer.Option(None, "--port", "-p", help="daemon 端口（覆盖 settings.daemon.port）"),
    session: str = typer.Option(None, "--session", "-s", help="attach 指定会话 id"),
    resume: bool = typer.Option(False, "--resume", help="恢复最近会话"),
    run: str = typer.Option(None, "--run", help="一次性模式：发单条任务后等 close 退出"),
) -> None:
    """连接 daemon 的 CLI 客户端（复用终端渲染 + HITL 回传）。"""
    from agent.daemon.client import run_client

    settings = load_settings()
    p = port or settings.daemon.port
    asyncio.run(run_client(p, session_id=session, resume=resume, run_task=run))


if __name__ == "__main__":
    app()
