"""M7 守护进程：WebSocket 服务 + 本地 HTTP /health。

- 仅绑定回环地址（``settings.daemon.host``，默认 ``127.0.0.1``）。
- 单 asyncio 事件循环驱动所有 ``Session``；多会话并发靠**每会话 Lock**（见 registry）。
- 消息路由见 ``_route``：hello / session.* / task.send / answer / confirm_plan / approve / command。
- 后台子 agent 挂在 daemon 单循环，无人 attach 时仍推进，事件进缓冲、attach 后回放（M7.4）。

websockets 仅在本模块 import（仅 daemon 路径），不影响 run / chat 进程内入口。
"""

from __future__ import annotations

import asyncio
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import TYPE_CHECKING, Any

import websockets  # 仅 daemon 路径 import

from agent.daemon.protocol import (
    DAEMON_VERSION,
    PROTOCOL_VERSION,
    MsgType,
    WsConnection,
    make_message,
    parse_message,
)
from agent.daemon.registry import SessionHandle, SessionRegistry

if TYPE_CHECKING:
    from agent.config.settings import Settings
    from agent.core.session import Session
    from agent.daemon.bridge import BridgeTransport


class Connection:
    """对单个前端 WebSocket 连接的轻量包装。"""

    def __init__(self, ws: WsConnection) -> None:
        self.ws = ws
        self.session_id: str | None = None
        # 串行化出站消息：保证 FINAL 事件先于 CLOSE 到达（避免 CLOSE 抢占事件）。
        self._lock = asyncio.Lock()

    async def send(
        self,
        type: MsgType | str,
        payload: dict[str, Any] | None = None,
        *,
        id: str | None = None,
        session: str | None = None,
    ) -> None:
        async with self._lock:
            await self.ws.send(make_message(type, payload, id=id, session=session))


# --------------------------------------------------------------------------- #
# 默认会话 / 传输工厂（真实 daemon 使用；测试可注入 fake）
# --------------------------------------------------------------------------- #
def _default_session_factory(settings: Settings, store, session_id: str) -> Session:
    from agent.core.model import create_model
    from agent.core.session import Session
    from agent.obs.store import TraceStore
    from agent.obs.tracer import Tracer
    from agent.runtime.registry import default_registry

    tracer = Tracer() if settings.obs.enabled else None
    model = create_model(settings, tracer=tracer)
    trace_store = TraceStore(settings.obs.db_path) if settings.obs.enabled else None
    # M6.2 冷启动：该 session_id 已存在于 sqlite → 从 store 恢复（重建 messages + event_stream）；
    # 否则新建（并落初始行）。同一工厂同时服务新建与恢复两条路径。
    if store.get_session(session_id) is not None:
        return Session.from_store(model, default_registry, settings, store, session_id, tracer=tracer, trace_store=trace_store)
    return Session(
        model,
        default_registry,
        settings,
        tracer,
        plan_mode=settings.plan.mode,
        trace_store=trace_store,
        session_id=session_id,
        session_store=store,
    )


def _default_transport_factory(handle: SessionHandle) -> BridgeTransport:
    from agent.daemon.bridge import BridgeTransport

    return BridgeTransport(handle)


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
async def _handler(ws: WsConnection, registry: SessionRegistry) -> None:
    conn = Connection(ws)
    try:
        async for raw in ws:
            msg = parse_message(raw)
            mtype = msg.get("type")
            payload = msg.get("payload") or {}
            mid = msg.get("id")
            try:
                await _route(conn, registry, mtype, payload, mid)
            except Exception as e:  # 单条消息处理异常不影响连接
                await conn.send(MsgType.ERROR, {"code": "handler_error", "message": str(e)})
    finally:
        registry.detach(conn)


async def _route(conn: Connection, registry: SessionRegistry, mtype: str | None, payload: dict[str, Any], _mid: str | None) -> None:
    if mtype == MsgType.HELLO.value:
        token = payload.get("token", "")
        expected = getattr(registry, "_token", "") or ""
        if expected and token != expected:
            await conn.send(MsgType.ERROR, {"code": "auth", "message": "token mismatch"})
            return
        await conn.send(
            MsgType.WELCOME,
            {"daemon_version": DAEMON_VERSION, "protocol_version": PROTOCOL_VERSION},
        )
    elif mtype == MsgType.SESSION_NEW.value:
        handle = registry.new(name=payload.get("name"))
        conn.session_id = handle.session_id
        handle.attached_conn = conn
        await conn.send(
            MsgType.SESSION_CREATED,
            {"session_id": handle.session_id, "name": handle.name},
            session=handle.session_id,
        )
        await conn.send(MsgType.ATTACHED, {"session_id": handle.session_id}, session=handle.session_id)
    elif mtype == MsgType.SESSION_ATTACH.value:
        await _attach(conn, registry, payload.get("session_id"))
    elif mtype == MsgType.SESSION_SWITCH.value:
        await _switch(conn, registry, payload.get("session_id"))
    elif mtype == MsgType.SESSION_DETACH.value:
        sid = registry.detach(conn)
        await conn.send(MsgType.DETACHED, {"session_id": sid})
    elif mtype == MsgType.SESSION_LIST.value:
        await conn.send(MsgType.SESSION_LIST_RESP, {"sessions": registry.list_info()})
    elif mtype == MsgType.TASK_SEND.value:
        await _task_send(conn, registry, payload.get("text", ""), yes=payload.get("yes", False), _plan=payload.get("plan", False))
    elif mtype == MsgType.ANSWER.value:
        _resolve(conn, registry, payload.get("id"), payload.get("text", ""))
    elif mtype == MsgType.CONFIRM_PLAN.value:
        _resolve(conn, registry, payload.get("id"), bool(payload.get("confirmed", False)))
    elif mtype == MsgType.APPROVE.value:
        _resolve(conn, registry, payload.get("id"), bool(payload.get("approved", False)))
    elif mtype == MsgType.COMMAND.value:
        await _command(conn, registry, payload.get("name", ""), payload.get("args"))
    else:
        await conn.send(MsgType.ERROR, {"code": "unknown_type", "message": mtype or ""})


async def _attach(conn: Connection, registry: SessionRegistry, sid: str | None) -> None:
    handle = registry.attach(conn, sid or "")
    if handle is None:
        await conn.send(MsgType.ERROR, {"code": "no_session", "message": sid or ""})
        return
    await conn.send(MsgType.ATTACHED, {"session_id": sid}, session=sid)
    await _replay(conn, handle, sid)


async def _switch(conn: Connection, registry: SessionRegistry, sid: str | None) -> None:
    handle = registry.switch(conn, sid or "")
    if handle is None:
        await conn.send(MsgType.ERROR, {"code": "no_session", "message": sid or ""})
        return
    await conn.send(MsgType.ATTACHED, {"session_id": sid}, session=sid)
    await _replay(conn, handle, sid)


async def _replay(conn: Connection, handle: SessionHandle, sid: str | None) -> None:
    """M7.4：先发 replay_start，再批量补发最近 K 条**持久化**事件，最后 replay_end。

    缓冲仅含非 transient 事件（见 BridgeTransport._on_event），故 tool_call_delta 等瞬时
    事件不会重画，避免参数预览重复渲染。
    """
    await conn.send(MsgType.REPLAY_START, {}, session=sid)
    for ev in list(handle.event_buffer):
        await conn.send(MsgType.EVENT, {"event": ev.to_dict()}, session=sid)
    await conn.send(MsgType.REPLAY_END, {}, session=sid)


async def _task_send(
    conn: Connection,
    registry: SessionRegistry,
    text: str,
    *,
    yes: bool,
    _plan: bool,
) -> None:
    sid = conn.session_id
    if sid is None:
        await conn.send(MsgType.ERROR, {"code": "no_session", "message": "attach first"})
        return
    handle = registry.get(sid)
    if handle is None:
        await conn.send(MsgType.ERROR, {"code": "no_session", "message": sid})
        return
    if handle.session is None:
        await conn.send(MsgType.ERROR, {"code": "no_session", "message": "session not initialized"})
        return
    if handle.transport is None:
        await conn.send(MsgType.ERROR, {"code": "no_transport", "message": "session has no transport"})
        return
    if handle.busy:
        await conn.send(MsgType.ERROR, {"code": "busy", "message": "session is busy"})
        return
    # 捕获已 narrowing 的 session/transport，供闭包 _run 使用（避免跨闭包丢失类型收窄）。
    session = handle.session
    transport = handle.transport
    # 同步置 busy：避免并发 task.send 竞态（配合每会话 Lock 双重保险）。
    handle.busy = True

    async def _run() -> None:
        handle.running = True
        handle.last_activity = time.time()
        try:
            async with handle.lock:  # 每会话串行化（即便 busy 被绕过也安全）
                res, _err = await session.step(
                    text, transport, yes=yes, fatal_plan_decline=False
                )
            # 等待所有在飞事件转发完成，保证 FINAL 等事件先于 CLOSE 落地（顺序正确性）。
            await transport.flush()
            if res is not None:
                transport.report_usage(res.usage, res.text)
        except Exception as e:  # step 异常优雅处理，不断开连接
            transport.notify(f"step error: {type(e).__name__}: {e}")
        finally:
            handle.running = False
            handle.busy = False
            try:
                await conn.send(MsgType.CLOSE, {}, session=sid)
            except Exception:
                pass

    asyncio.ensure_future(_run())


def _resolve(conn: Connection, registry: SessionRegistry, rid: str | None, value: object) -> None:
    sid = conn.session_id
    if sid is None or rid is None:
        return
    handle = registry.get(sid)
    if handle is not None and handle.transport is not None:
        handle.transport.resolve(rid, value)


async def _command(conn: Connection, registry: SessionRegistry, name: str, args: str | None) -> None:
    from agent.core.session_command import dispatch_command  # 延迟导入（M7.5 才落地）

    sid = conn.session_id
    handle = registry.get(sid) if sid else None
    if handle is None:
        await conn.send(MsgType.ERROR, {"code": "no_session", "message": sid})
        return
    if handle.session is None:
        await conn.send(MsgType.ERROR, {"code": "no_session", "message": "session not initialized"})
        return
    if handle.transport is None:
        await conn.send(MsgType.ERROR, {"code": "no_transport", "message": "session has no transport"})
        return
    if name == "switch":
        await _switch(conn, registry, args)
        return
    raw = f"/{name}" + (f" {args}" if args else "")
    handled = await dispatch_command(handle.session, raw, handle.transport, handle.session.settings)
    if not handled:
        handle.transport.notify(f"未知命令: {raw}")


# --------------------------------------------------------------------------- #
# 启停
# --------------------------------------------------------------------------- #
def create_ws_server(registry: SessionRegistry, host: str, port: int):
    """创建 WebSocket 服务（返回 websockets server，便于测试在临时端口启动）。"""

    async def _h(ws: WsConnection):
        await _handler(ws, registry)

    return websockets.serve(_h, host, port)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] in ("/health", "/health/"):
            body = json.dumps({"status": "ok", "daemon_version": DAEMON_VERSION}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # 静默
        pass


def _start_health_server(host: str, port: int) -> HTTPServer:
    httpd = HTTPServer((host, port), _HealthHandler)
    t = Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


async def _serve(settings: Settings, registry: SessionRegistry, stop_event: asyncio.Event) -> None:
    async with create_ws_server(registry, settings.daemon.host, settings.daemon.port):
        await stop_event.wait()


def start_daemon(settings: Settings) -> None:
    """启动守护进程：HTTP /health（独立端口）+ WebSocket 服务；直到 Ctrl-C。"""
    from agent.context.session_store import SessionStore

    store = SessionStore(settings.obs.sessions_db_path)  # M6.2 冷启动恢复的数据源
    registry = SessionRegistry(
        session_factory=lambda sid: _default_session_factory(settings, store, sid),
        transport_factory=_default_transport_factory,
        restore_factory=lambda sid: _default_session_factory(settings, store, sid),
    )
    registry._token = settings.daemon.token  # 供 hello 鉴权（可选）
    httpd = _start_health_server(settings.daemon.host, settings.daemon.health_port)
    stop = asyncio.Event()
    typer_echo = _safe_echo()
    typer_echo(
        f"[daemon] 已启动：ws=ws://{settings.daemon.host}:{settings.daemon.port} "
        f"health=http://{settings.daemon.host}:{settings.daemon.health_port}/health",
        err=True,
    )
    try:
        asyncio.run(_serve(settings, registry, stop))
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()


def _safe_echo():
    from typer import echo

    return lambda m, **k: echo(m, err=True)
