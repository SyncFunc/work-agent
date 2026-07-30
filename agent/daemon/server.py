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
import logging
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import TYPE_CHECKING, Any, cast

import websockets  # 仅 daemon 路径 import

from agent.context.tokens import _estimate_tokens
from agent.core.events import Event, EventStream, EventType
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
    from agent.core.loop import AgentResult
    from agent.core.session import Session
    from agent.daemon.bridge import BridgeTransport

log = logging.getLogger("agent.daemon")


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
def _anchor_path(p: str, project_root: str) -> str:
    """把相对 db 路径锚定到 ``project_root``（解决多项目隔离：相对路径默认相对 cwd 会串项目）。"""
    return p if os.path.isabs(p) else os.path.join(project_root, p)


def _default_transport_factory(handle: SessionHandle) -> BridgeTransport:
    from agent.daemon.bridge import BridgeTransport

    return BridgeTransport(handle)


def _emit_usage(
    stream: EventStream,
    res: AgentResult,
    duration: float,
    *,
    parent_message_id: str | None,
) -> None:
    """M10.2：把一次响应的 token 用量作为 USAGE 事件落盘（并实时转发）。

    替换原 ``transport.report_usage`` 的 ``MsgType.USAGE`` 实时路径（M10.3 删除）。
    usage 为空时退化为估算 token 数，标记 estimated=True（与历史 report_usage 一致）。
    """
    mid = res.message_id or stream.current_message_id
    if mid is None:
        return
    usage = res.usage
    if not usage:
        est = _estimate_tokens(res.text or "")
        stream.append(
            Event(
                type=EventType.USAGE,
                message_id=mid,
                parent_message_id=parent_message_id,
                usage={"estimated_tokens": est},
                duration=duration,
                estimated=True,
            )
        )
        return
    stream.append(
        Event(
            type=EventType.USAGE,
            message_id=mid,
            parent_message_id=parent_message_id,
            usage=dict(usage),
            duration=duration,
            estimated=False,
        )
    )


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


async def _route(
    conn: Connection,
    registry: SessionRegistry,
    mtype: str | None,
    payload: dict[str, Any],
    _mid: str | None,
) -> None:
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
        project_root = payload.get("project_root") or os.getcwd()
        handle = registry.new(project_root, name=payload.get("name"))
        conn.session_id = handle.session_id
        handle.attached_conn = conn
        await conn.send(
            MsgType.SESSION_CREATED,
            {"session_id": handle.session_id, "name": handle.name, "project_root": project_root},
            session=handle.session_id,
        )
        await conn.send(
            MsgType.ATTACHED,
            {"session_id": handle.session_id, "project_root": project_root},
            session=handle.session_id,
        )
    elif mtype == MsgType.SESSION_ATTACH.value:
        project_root = payload.get("project_root") or os.getcwd()
        await _attach(conn, registry, project_root, payload.get("session_id"))
    elif mtype == MsgType.SESSION_SWITCH.value:
        project_root = payload.get("project_root") or os.getcwd()
        await _switch(conn, registry, project_root, payload.get("session_id"))
    elif mtype == MsgType.SESSION_DETACH.value:
        sid = registry.detach(conn)
        await conn.send(MsgType.DETACHED, {"session_id": sid})
    elif mtype == MsgType.SESSION_LIST.value:
        project_root = payload.get("project_root") or os.getcwd()
        await conn.send(
            MsgType.SESSION_LIST_RESP,
            {"project_root": project_root, "sessions": registry.list_info(project_root)},
        )
    elif mtype == MsgType.TASK_SEND.value:
        await _task_send(
            conn,
            registry,
            payload.get("text", ""),
            yes=payload.get("yes", False),
            _plan=payload.get("plan", False),
        )
    elif mtype == MsgType.ANSWER.value:
        _resolve(conn, registry, payload.get("id"), payload.get("text", ""))
    elif mtype == MsgType.CONFIRM_PLAN.value:
        _resolve(conn, registry, payload.get("id"), bool(payload.get("confirmed", False)))
    elif mtype == MsgType.APPROVE.value:
        _resolve(conn, registry, payload.get("id"), bool(payload.get("approved", False)))
    elif mtype == MsgType.COMMAND.value:
        await _command(conn, registry, payload.get("name", ""), payload.get("args"))
    elif mtype == MsgType.TASK_CANCEL.value:
        await _task_cancel(conn, registry)
    elif mtype == MsgType.SESSION_DELETE.value:
        await _session_delete(
            conn, registry, payload.get("session_id"), payload.get("project_root")
        )
    elif mtype == MsgType.TRACE_LIST.value:
        await _trace_list(
            conn,
            registry,
            payload.get("project_root") or os.getcwd(),
            payload.get("session_id"),
            _mid,
        )
    elif mtype == MsgType.TRACE_GET.value:
        await _trace_get(
            conn,
            registry,
            payload.get("project_root") or os.getcwd(),
            payload.get("trace_id"),
            _mid,
        )
    else:
        await conn.send(MsgType.ERROR, {"code": "unknown_type", "message": mtype or ""})


async def _attach(
    conn: Connection, registry: SessionRegistry, project_root: str, sid: str | None
) -> None:
    handle = registry.attach(conn, project_root, sid or "")
    if handle is None:
        await conn.send(MsgType.ERROR, {"code": "no_session", "message": sid or ""})
        return
    await conn.send(
        MsgType.ATTACHED, {"session_id": sid, "project_root": project_root}, session=sid
    )
    await _send_session_info(conn, handle, sid)
    await _replay(conn, handle, sid)


async def _switch(
    conn: Connection, registry: SessionRegistry, project_root: str, sid: str | None
) -> None:
    handle = registry.switch(conn, project_root, sid or "")
    if handle is None:
        await conn.send(MsgType.ERROR, {"code": "no_session", "message": sid or ""})
        return
    await conn.send(
        MsgType.ATTACHED, {"session_id": sid, "project_root": project_root}, session=sid
    )
    await _send_session_info(conn, handle, sid)
    await _replay(conn, handle, sid)


async def _replay(conn: Connection, handle: SessionHandle, sid: str | None) -> None:
    """M7.4：先发 replay_start，再批量补发**持久化**事件，最后 replay_end。

    缓冲仅含非 transient 事件（见 BridgeTransport._on_event），故 tool_call_delta 等瞬时
    事件不会重画，避免参数预览重复渲染。

    M9 subsession：回放经 ``SessionStore.iter_events_with_subsession`` 取**全量**父会话事件 +
    其全部子会话事件（每条带 ``subsession_id``），前端据此在**主聊天区**重建独立子 agent 块历史。

    修复「重进后历史变少」：旧实现只用内存 ``event_buffer``（``maxlen=200``）回放父会话 + 子会话
    缓冲，长会话会被截断、子会话事件从未落盘（重启即丢）。现改为读 sqlite 全量，且子会话事件已带
    ``parent_session_id`` 持久化，重启后仍能按父会话恢复完整历史。
    """
    await conn.send(MsgType.REPLAY_START, {}, session=sid)
    # 优先用 sqlite 全量回放（含子会话）；无 store 时回退内存缓冲（CLI / 兼容）。
    store = None
    reg = handle.registry
    if reg is not None:
        factory = getattr(reg, "_store_factory", None)
        if factory is not None:
            try:
                store = factory(handle.project_root)
            except Exception:
                store = None
    if store is not None:
        for ev, sub in store.iter_events_with_subsession(sid):
            if sub is not None:
                await conn.send(
                    MsgType.EVENT,
                    {"event": ev.to_dict(), "subsession_id": sub},
                    session=sid,
                )
            else:
                await conn.send(MsgType.EVENT, {"event": ev.to_dict()}, session=sid)
    else:
        # 回退：内存缓冲（无持久化场景）。
        for ev in list(handle.event_buffer):
            await conn.send(MsgType.EVENT, {"event": ev.to_dict()}, session=sid)
        if reg is not None:
            for cid in list(handle.children):
                sub = reg.get_subsession(cid)
                if sub is None:
                    continue
                for ev in list(sub.event_buffer):
                    await conn.send(
                        MsgType.EVENT,
                        {"event": ev.to_dict(), "subsession_id": cid},
                        session=sid,
                    )
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
        await conn.send(
            MsgType.ERROR, {"code": "no_transport", "message": "session has no transport"}
        )
        return
    if handle.busy:
        await conn.send(MsgType.ERROR, {"code": "busy", "message": "session is busy"})
        return
    # 捕获已 narrowing 的 session/transport，供闭包 _run 使用（避免跨闭包丢失类型收窄）。
    # session 在 daemon 中实际为 Session 实例，但 SessionHandle.session 标注为 SessionLike，
    # 故用 cast 让 basedpyright 识别 event_stream（M10.2 落盘入口）。
    session = cast("Session", handle.session)
    transport = handle.transport
    # 同步置 busy：避免并发 task.send 竞态（配合每会话 Lock 双重保险）。
    handle.busy = True
    trace_id = uuid.uuid4().hex[:12]  # M10.6：每条用户消息分配独立 trace_id 串联整体链路

    async def _run() -> None:
        handle.running = True
        handle.last_activity = time.time()
        try:
            t0 = time.time()
            async with handle.lock:  # 每会话串行化（即便 busy 被绕过也安全）
                res, _err = await session.step(
                    text, transport, yes=yes, fatal_plan_decline=False, trace_id=trace_id
                )
            duration = time.time() - t0
            # step 内可能切换了 plan_mode（计划批准后 → False），通知前端更新
            await _send_session_info(conn, handle, sid)
            # 等待所有在飞事件转发完成，保证 FINAL 等事件先于 CLOSE 落地（顺序正确性）。
            await transport.flush()
            if res is not None:
                # M10.2：顶层 message 用量改为 append USAGE 事件（落盘 + 经 event 消息实时转发），
                # 替换原 transport.report_usage 的 MsgType.USAGE 实时路径（M10.3 删除）。
                _emit_usage(session.event_stream, res, duration, parent_message_id=None)
        except asyncio.CancelledError:
            # M9.9 真实取消：task.cancel() 在此被捕获，向客户端宣告已停止。
            handle.cancel_requested = False
            transport.notify("已停止生成")
            try:
                await conn.send(MsgType.TASK_CANCELLED, {}, session=sid)
            except Exception:
                pass
            raise  # 交还 finally 清状态并发送 CLOSE
        except Exception as e:  # step 异常优雅处理，不断开连接
            transport.notify(f"step error: {type(e).__name__}: {e}")
        finally:
            handle.running = False
            handle.busy = False
            handle.running_task = None
            try:
                await conn.send(MsgType.CLOSE, {}, session=sid)
            except Exception:
                pass

    task = asyncio.ensure_future(_run())
    handle.running_task = task


def _resolve(conn: Connection, registry: SessionRegistry, rid: str | None, value: object) -> None:
    sid = conn.session_id
    if sid is None or rid is None:
        return
    handle = registry.get(sid)
    if handle is not None and handle.transport is not None:
        handle.transport.resolve(rid, value)


async def _task_cancel(conn: Connection, registry: SessionRegistry) -> None:
    """M9.9 真实取消：取消当前会话在飞的 step 任务（task.cancel() 中断 LLM 流）。"""
    sid = conn.session_id
    if sid is None:
        return
    handle = registry.get(sid)
    if handle is None or handle.running_task is None:
        return
    handle.cancel_requested = True
    handle.running_task.cancel()


async def _send_session_info(conn: Connection, handle: SessionHandle, sid: str | None) -> None:
    """M9.9 会话状态推送：plan_mode / model（前端顶栏与输入区展示）。"""
    session = handle.session
    if session is None:
        return
    plan_mode = bool(getattr(session, "plan_mode", False))
    model = ""
    try:
        model = session.settings.llm.model
    except Exception:
        model = ""
    await conn.send(MsgType.SESSION_INFO, {"plan_mode": plan_mode, "model": model}, session=sid)


async def _session_delete(
    conn: Connection, registry: SessionRegistry, sid: str | None, project_root: str | None
) -> None:
    """M9.9 彻底删除会话（含事件 / Session Memory / trace + 内存句柄级联）。"""
    if not sid:
        await conn.send(MsgType.SESSION_DELETE_RESP, {"ok": False, "message": "missing session_id"})
        return
    handle = registry.get(sid)
    proj = project_root or (handle.project_root if handle is not None else os.getcwd())
    settings = None
    try:
        from agent.config.settings import load_settings

        settings = load_settings(project_root=proj)
    except Exception:
        settings = None

    store_factory = getattr(registry, "_store_factory", None)
    trace_factory = getattr(registry, "_trace_store_factory", None)
    store = store_factory(proj) if store_factory else None
    trace_store = trace_factory(proj) if trace_factory else None
    session_memory_dir = (
        os.path.join(proj, settings.context.session_memory_dir) if settings is not None else None
    )

    try:
        if store is not None:
            store.delete_session(
                sid, trace_store=trace_store, session_memory_dir=session_memory_dir
            )
        elif trace_store is not None:
            trace_store.delete_session(sid)
    except Exception as e:
        await conn.send(MsgType.SESSION_DELETE_RESP, {"ok": False, "message": str(e)}, session=sid)
        return

    # 内存句柄清理（含级联子会话）
    if registry.get(sid) is not None:
        registry.cascade_remove(sid)
    else:
        registry.unregister_subsession(sid)

    await conn.send(MsgType.SESSION_DELETE_RESP, {"ok": True, "session_id": sid}, session=sid)


async def _command(
    conn: Connection, registry: SessionRegistry, name: str, args: str | None
) -> None:
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
        await conn.send(
            MsgType.ERROR, {"code": "no_transport", "message": "session has no transport"}
        )
        return
    if name == "switch":
        cur = registry.get(conn.session_id)
        project_root = cur.project_root if cur is not None else os.getcwd()
        await _switch(conn, registry, project_root, args)
        return
    raw = f"/{name}" + (f" {args}" if args else "")
    handled = await dispatch_command(handle.session, raw, handle.transport, handle.session.settings)
    if not handled:
        handle.transport.notify(f"未知命令: {raw}")


# --------------------------------------------------------------------------- #
# M9.7 可观测面板：trace 查询（按 project_root 隔离读取 TraceStore）
# --------------------------------------------------------------------------- #
def _span_to_dict(s: Any) -> dict[str, Any]:
    """把一个 Span 序列化为可 JSON 化的节点（含父子关系与日志）。"""
    return {
        "span_id": s.id,
        "name": s.name,
        "kind": s.kind,
        "parent_id": s.parent_id,
        "started_at": s.started_at,
        "ended_at": s.ended_at,
        "status": "open" if s.ended_at is None else "ok",
        "meta": s.meta,
        "logs": [
            {"ts": lg.ts, "key": lg.key, "value": lg.value, "level": lg.level} for lg in s.logs
        ],
    }


async def _trace_list(
    conn: Connection,
    registry: SessionRegistry,
    project_root: str,
    session_id: str | None,
    mid: str | None,
) -> None:
    factory = getattr(registry, "_trace_store_factory", None)
    if factory is None:
        await conn.send(
            MsgType.TRACE_LIST_RESP, {"project_root": project_root, "traces": []}, id=mid
        )
        return
    try:
        traces = factory(project_root).list_traces(session_id)
    except Exception as e:  # 存储不可用：退化为空列表，不阻断查询
        await conn.send(MsgType.ERROR, {"code": "trace_error", "message": str(e)}, id=mid)
        return
    await conn.send(
        MsgType.TRACE_LIST_RESP, {"project_root": project_root, "traces": traces}, id=mid
    )


async def _trace_get(
    conn: Connection,
    registry: SessionRegistry,
    project_root: str,
    trace_id: str | None,
    mid: str | None,
) -> None:
    if not trace_id:
        await conn.send(MsgType.TRACE_TREE, {"session_id": trace_id, "spans": []}, id=mid)
        return
    factory = getattr(registry, "_trace_store_factory", None)
    if factory is None:
        await conn.send(MsgType.TRACE_TREE, {"session_id": trace_id, "spans": []}, id=mid)
        return
    try:
        tracer = factory(project_root).load_trace(trace_id)
    except Exception as e:
        await conn.send(MsgType.ERROR, {"code": "trace_error", "message": str(e)}, id=mid)
        return
    if tracer is None:
        await conn.send(MsgType.TRACE_TREE, {"session_id": trace_id, "spans": []}, id=mid)
        return
    spans = [_span_to_dict(s) for s in tracer.spans]
    await conn.send(MsgType.TRACE_TREE, {"session_id": trace_id, "spans": spans}, id=mid)


# --------------------------------------------------------------------------- #
# 启停
# --------------------------------------------------------------------------- #
def create_ws_server(registry: SessionRegistry, host: str, port: int):
    """创建 WebSocket 服务（返回 websockets server，便于测试在临时端口启动）。"""

    async def _h(ws: WsConnection):
        await _handler(ws, registry)

    return websockets.serve(_h, host, port, reuse_address=True)


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


async def _prewarm() -> None:
    """后台预热：daemon 就绪后尽快加载注册表与默认模型客户端，

    消除首次 attach 会话时的冷启动延迟（provider 模块导入 / 客户端构造 / 设置解析）。
    任何失败（如缺少 api_key）仅记录调试日志，绝不影响主流程。
    """

    try:
        from agent.config.settings import load_settings
        from agent.core.model import create_model
        from agent.runtime.registry import default_registry

        _ = default_registry  # 触发内置工具/规格注册（进程级缓存）
        try:
            settings = load_settings()
        except Exception as exc:  # 解析默认项目设置失败则跳过
            log.debug("[daemon] 预热 load_settings 跳过: %s", exc)
            settings = None
        if settings is not None:
            try:
                create_model(settings, tracer=None)
            except Exception as exc:  # 无 api_key 等情况：跳过模型预热
                log.debug("[daemon] 预热 create_model 跳过: %s", exc)
        log.info("[daemon] 预热完成：注册表与默认模型客户端已就绪")
    except Exception as exc:  # 任何意外都不应阻断主流程
        log.debug("[daemon] 预热跳过: %s", exc)


async def _serve(settings: Settings, registry: SessionRegistry, stop_event: asyncio.Event) -> None:
    async with create_ws_server(registry, settings.daemon.host, settings.daemon.port):
        # 此处已进入 async with：WebSocket 服务真正开始监听后再宣告就绪，
        # 避免 M9.1 DaemonManager.waitForReady 在端口尚未可连接时误判就绪。
        _safe_echo()(
            f"[daemon] 已启动（多项目感知）：ws=ws://{settings.daemon.host}:{settings.daemon.port} "
            f"health=http://{settings.daemon.host}:{settings.daemon.health_port}/health",
            err=True,
        )
        # 后台预热：避免首次 attach 会话的冷启动延迟（见 _prewarm）。
        asyncio.create_task(_prewarm())
        await stop_event.wait()


def start_daemon(settings: Settings) -> None:
    """启动守护进程：HTTP /health（独立端口）+ WebSocket 服务；直到 Ctrl-C。

    M9.0 多项目感知：``settings`` 仅用于 daemon **网络配置**（host/port/health_port/token），
    不再隐式绑定任何项目根。每个会话在运行时按各自的 ``project_root`` 经
    ``load_settings(project_root=...)`` 解析项目级 settings，并按项目隔离 ``SessionStore``
    （db 路径锚定到 ``project_root``，避免多项目串扰）。
    """
    from agent.config.settings import load_settings
    from agent.context.session_store import SessionStore
    from agent.core.model import create_model
    from agent.core.session import Session
    from agent.obs.span_log_handler import ensure_span_log_handler
    from agent.obs.store import TraceStore
    from agent.obs.tracer import Tracer
    from agent.runtime.registry import default_registry

    ensure_span_log_handler()

    # 按 project_root 惰性解析并缓存 SessionStore（同一项目复用同一个 store 实例）。
    store_cache: dict[str, SessionStore] = {}

    def store_for(project_root: str) -> SessionStore:
        cached = store_cache.get(project_root)
        if cached is not None:
            return cached
        s = load_settings(project_root=project_root)
        db = _anchor_path(s.obs.sessions_db_path, project_root)
        store = SessionStore(db)
        store_cache[project_root] = store
        return store

    # 按 project_root 惰性解析并缓存 TraceStore（与 store_for 对称；M9.7 可观测面板查询用）。
    trace_store_cache: dict[str, TraceStore] = {}

    def trace_store_for(project_root: str) -> TraceStore:
        cached = trace_store_cache.get(project_root)
        if cached is not None:
            return cached
        s = load_settings(project_root=project_root)
        trace_db = _anchor_path(s.obs.db_path, project_root)
        store = TraceStore(trace_db)
        trace_store_cache[project_root] = store
        return store

    def _build_session(project_root: str, session_id: str, store: SessionStore) -> Session:
        s = load_settings(project_root=project_root)
        tracer = Tracer() if s.obs.enabled else None
        model = create_model(s, tracer=tracer)
        trace_db = _anchor_path(s.obs.db_path, project_root)
        trace_store = TraceStore(trace_db) if s.obs.enabled else None
        # M6.2 冷启动：该 session_id 已存在于 sqlite → 从 store 恢复（重建 messages + event_stream）；
        # 否则新建（并落初始行）。同一工厂同时服务新建与恢复两条路径。
        if store.get_session(session_id) is not None:
            return Session.from_store(
                model,
                default_registry,
                s,
                store,
                session_id,
                tracer=tracer,
                trace_store=trace_store,
                project_root=project_root,
            )
        return Session(
            model,
            default_registry,
            s,
            tracer,
            plan_mode=s.plan.mode,
            trace_store=trace_store,
            session_id=session_id,
            session_store=store,
            project_root=project_root,
        )

    def session_factory(project_root: str, session_id: str) -> Session:
        return _build_session(project_root, session_id, store_for(project_root))

    def restore_factory(project_root: str, session_id: str):
        store = store_for(project_root)
        if store.get_session(session_id) is not None:
            return _build_session(project_root, session_id, store)
        return None

    registry = SessionRegistry(
        session_factory=session_factory,
        transport_factory=_default_transport_factory,
        restore_factory=restore_factory,
        store_factory=store_for,
        trace_store_factory=trace_store_for,
    )
    registry._token = settings.daemon.token  # 供 hello 鉴权（可选）
    httpd = _start_health_server(settings.daemon.host, settings.daemon.health_port)
    stop = asyncio.Event()
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
