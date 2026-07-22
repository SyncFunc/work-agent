"""M7.3 CLI 客户端：连 daemon，复用 ``TerminalTransport`` 渲染 + HITL 回传。

客户端是「哑渲染 + 输入」端：
- 每个收到的 ``event`` 消息：``transport._on_event(Event.from_dict(payload["event"]))``，
  直接复用既有事件→rich 渲染映射（``TerminalTransport`` 零改动）。
- 收到 HITL 请求（``ask`` / ``confirm_plan`` / ``approve``）：就地提问并回传同 ``id`` 应答，
  daemon 侧 ``BridgeTransport`` 被唤醒继续 ``Session.step``。

``run_client`` 为入口；``_run(ws, ...)`` 抽出以便测试注入假 ws 连接。
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import websockets  # 仅 daemon/client 路径 import

from agent.config.settings import load_settings
from agent.core.events import Event, EventStream
from agent.core.intent import Question
from agent.core.loop import AgentResult
from agent.core.plan import PlanStep
from agent.daemon.protocol import MsgType, WsConnection, make_message, parse_message
from agent.runtime.approval import Action
from agent.runtime.terminal_transport import TerminalTransport

if TYPE_CHECKING:
    from agent.config.settings import Settings


def _dict_to_action(d: dict[str, Any]) -> Action:
    return Action(
        tool=d.get("tool", ""),
        risk=d.get("risk", ""),
        args=d.get("args", {}),
        description=d.get("description", ""),
        approval_request=bool(d.get("approval_request", False)),
    )


def _fake_plan_result(p: dict[str, Any]) -> AgentResult:
    return AgentResult(
        text="",
        events=EventStream(),
        iterations=0,
        plan=p.get("plan"),
        plan_path=p.get("plan_path"),
        plan_steps=[PlanStep(**s) for s in p.get("plan_steps", [])],
    )


def _dict_to_spec(d: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**d)


def _parse_command(line: str) -> tuple[str, str | None]:
    """把 ``/name args`` 拆为 (name, args)。供测试与 REPL 复用。"""
    s = line.strip()
    if not s.startswith("/"):
        return "", None
    rest = s[1:]
    idx = rest.find(" ")
    if idx < 0:
        return rest, None
    return rest[:idx], rest[idx + 1:].strip() or None


async def _prompt_line(transport: TerminalTransport) -> str:
    if transport.interactive and sys.stdin.isatty():
        from prompt_toolkit import PromptSession

        ptk = PromptSession()
        return await ptk.prompt_async("you> ")
    import typer

    return typer.prompt("you")


async def _run(
    ws: WsConnection,
    *,
    session_id: str | None = None,
    resume: bool = False,
    run_task: str | None = None,
    settings: "Settings | None" = None,
    transport: TerminalTransport | None = None,
) -> None:
    settings = settings or load_settings()
    if transport is None:
        transport = TerminalTransport(interactive=sys.stdin.isatty(), context_mgr=None)
    current: str | None = None

    async def _recv() -> None:
        nonlocal current
        async for raw in ws:
            msg = parse_message(raw)
            t = msg.get("type")
            p = msg.get("payload") or {}
            if t == MsgType.EVENT.value:
                transport._on_event(Event.from_dict(p["event"]))
            elif t == MsgType.ASK.value:
                q = Question(**p["question"])
                ans = await transport.ask(q)
                await ws.send(make_message(MsgType.ANSWER, {"id": p["id"], "text": ans}, id=p["id"]))
            elif t == MsgType.CONFIRM_PLAN.value:
                confirmed = await transport.confirm_plan()
                await ws.send(
                    make_message(MsgType.CONFIRM_PLAN, {"id": p["id"], "confirmed": confirmed}, id=p["id"])
                )
            elif t == MsgType.APPROVE.value:
                approved = await transport.approve(_dict_to_action(p["action"]))
                await ws.send(
                    make_message(MsgType.APPROVE, {"id": p["id"], "approved": approved}, id=p["id"])
                )
            elif t == MsgType.SHOW_QUESTIONS.value:
                transport.show_questions([Question(**q) for q in p.get("questions", [])])
            elif t == MsgType.SHOW_PLAN.value:
                transport.show_plan(_fake_plan_result(p))
            elif t == MsgType.SHOW_SKILLS.value:
                transport.show_skills([_dict_to_spec(s) for s in p.get("specs", [])])
            elif t == MsgType.SHOW_AGENTS.value:
                transport.show_agents([_dict_to_spec(s) for s in p.get("specs", [])])
            elif t == MsgType.NOTIFY.value:
                transport.notify(p.get("message", ""))
            elif t == MsgType.USAGE.value:
                transport.report_usage(p.get("usage"), None)
            elif t == MsgType.WELCOME.value:
                pass
            elif t == MsgType.SESSION_CREATED.value:
                current = p["session_id"]
            elif t == MsgType.ATTACHED.value:
                current = p["session_id"]
            elif t == MsgType.CLOSE.value:
                transport.close()
                if run_task is not None:
                    break  # 一次性 --run 模式：收到 close 即退出
            elif t == MsgType.ERROR.value:
                transport.notify(f"[error] {p.get('message')}")

    recv_task = asyncio.ensure_future(_recv())

    # 握手
    await ws.send(make_message(MsgType.HELLO, {"client_type": "cli", "version": "0.1.0"}))
    if session_id:
        await ws.send(make_message(MsgType.SESSION_ATTACH, {"session_id": session_id}))
    elif resume:
        # 恢复最近会话：取 list 第一个
        await ws.send(make_message(MsgType.SESSION_LIST))
        # 简化：resume 时直接用 session.new（daemon 当前不持久化历史会话清单到磁盘）
        await ws.send(make_message(MsgType.SESSION_NEW))
    else:
        await ws.send(make_message(MsgType.SESSION_NEW))

    if run_task is not None:
        await ws.send(make_message(MsgType.TASK_SEND, {"text": run_task}))
        await recv_task
        return

    # 交互 REPL
    try:
        while True:
            line = await _prompt_line(transport)
            cmd = line.strip().lower()
            if cmd in {"exit", "quit"}:
                break
            if line.strip().startswith("/"):
                name, args = _parse_command(line)
                await ws.send(make_message(MsgType.COMMAND, {"name": name, "args": args}))
            elif line.strip():
                await ws.send(make_message(MsgType.TASK_SEND, {"text": line.strip()}))
    finally:
        recv_task.cancel()


async def run_client(
    port: int,
    *,
    host: str = "127.0.0.1",
    session_id: str | None = None,
    resume: bool = False,
    run_task: str | None = None,
) -> None:
    settings = load_settings()
    uri = f"ws://{host}:{port}"
    async with websockets.connect(uri) as ws:
        await _run(
            ws,
            session_id=session_id,
            resume=resume,
            run_task=run_task,
            settings=settings,
        )
