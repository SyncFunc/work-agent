"""M7 daemon 测试：SessionRegistry 单元 + WebSocket 端到端（BridgeTransport + HITL + 回放 + busy）。

使用真实 websockets 在临时端口起服务，配合 FakeSession（经 BridgeTransport 驱动 HITL future）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import websockets

from agent.config.settings import load_settings
from agent.core.events import Event, EventType, EventStream
from agent.core.intent import Question
from agent.core.loop import AgentResult
from agent.core.model import Decision
from agent.daemon.bridge import BridgeTransport
from agent.daemon.protocol import MsgType, make_message, parse_message
from agent.daemon.registry import SessionRegistry, SessionHandle
from agent.daemon.server import create_ws_server


class FakeSession:
    """最小可驱动会话：发出 TEXT + 瞬时 TOOL_CALL_DELTA + 向 transport 提问 + FINAL。"""

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id
        self.plan_mode = False
        self.plan_path = None
        self.context_mgr = None
        self.skill_loader = None
        self.messages: list = []
        self.loop = SimpleNamespace(_agent_span=None)
        self.settings = load_settings()

    async def step(self, task, transport, *, yes=False, fatal_plan_decline=False):
        stream = EventStream()
        transport.bind(stream)
        stream.append(Event(type=EventType.TEXT, text="thinking"))
        stream.emit(Event(type=EventType.TOOL_CALL_DELTA, tc_index=0, tc_name="write", tc_args='{"x":'))
        ans = await transport.ask(Question("choose", options=["a", "b"]))
        stream.append(Event(type=EventType.FINAL, text=f"ans={ans}"))
        return AgentResult(
            text=f"ans={ans}",
            events=stream,
            iterations=1,
            usage={"total_tokens": 5},
        ), None


def _make_registry() -> SessionRegistry:
    return SessionRegistry(
        session_factory=lambda sid: FakeSession(sid),
        transport_factory=lambda h: BridgeTransport(h),
    )


@pytest.fixture
async def server():
    registry = _make_registry()
    srv = await create_ws_server(registry, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    yield registry, port
    srv.close()
    await srv.wait_closed()


async def _recv_type(ws, mtype, timeout=3.0):
    """从 ws 读取下一条匹配 mtype 的消息（跳过其它类型）。"""
    async def _loop():
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout)
            d = parse_message(raw)
            if d["type"] == mtype.value:
                return d
    return await _loop()


async def _handshake(ws):
    await ws.send(make_message(MsgType.HELLO))
    return await _recv_type(ws, MsgType.WELCOME)


async def _new_session(ws):
    await ws.send(make_message(MsgType.SESSION_NEW))
    created = await _recv_type(ws, MsgType.SESSION_CREATED)
    await _recv_type(ws, MsgType.ATTACHED)
    return created["payload"]["session_id"]


async def _drive(ws, *, answer="a"):
    """持续接收：遇到 HITL 请求自动应答，遇到 CLOSE 停止；返回收集到的全部消息。"""
    collected = []
    while True:
        raw = await asyncio.wait_for(ws.recv(), 3.0)
        d = parse_message(raw)
        collected.append(d)
        if d["type"] == MsgType.ASK.value:
            await ws.send(make_message(MsgType.ANSWER, {"id": d["id"], "text": answer}, id=d["id"]))
        elif d["type"] == MsgType.CONFIRM_PLAN.value:
            await ws.send(make_message(MsgType.CONFIRM_PLAN, {"id": d["id"], "confirmed": True}, id=d["id"]))
        elif d["type"] == MsgType.APPROVE.value:
            await ws.send(make_message(MsgType.APPROVE, {"id": d["id"], "approved": True}, id=d["id"]))
        elif d["type"] == MsgType.CLOSE.value:
            break
    return collected


# --------------------------------------------------------------------------- #
# SessionRegistry 单元
# --------------------------------------------------------------------------- #
def test_registry_attach_switch_list():
    reg = SessionRegistry()
    h1 = reg.new(name="a")
    h2 = reg.new(name="b")

    class Conn:
        def __init__(self):
            self.session_id = None

        async def send(self, *a, **k):
            return None

    c = Conn()
    assert reg.attach(c, h1.session_id) is h1
    assert h1.attached_conn is c
    reg.switch(c, h2.session_id)
    assert h2.attached_conn is c
    assert h1.attached_conn is None
    info = reg.list_info()
    assert len(info) == 2
    assert {i["id"] for i in info} == {h1.session_id, h2.session_id}
    assert reg.detach(c) == h2.session_id
    assert h2.attached_conn is None


async def test_per_session_lock_serializes():
    reg = SessionRegistry()
    h = reg.new()
    order: list[int] = []

    async def work(n: int) -> None:
        async with h.lock:
            order.append(n)
            await asyncio.sleep(0.05)

    await asyncio.gather(work(1), work(2))
    assert order in ([1, 2], [2, 1])


# --------------------------------------------------------------------------- #
# WebSocket 端到端
# --------------------------------------------------------------------------- #
async def test_hello_welcome(server):
    _, port = server
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        w = await _handshake(ws)
        assert w["payload"]["protocol_version"] == "1.0"
        assert w["payload"]["daemon_version"]


async def test_session_new_and_list(server):
    _, port = server
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await _handshake(ws)
        sid = await _new_session(ws)
        await ws.send(make_message(MsgType.SESSION_LIST))
        lst = await _recv_type(ws, MsgType.SESSION_LIST_RESP)
        sess = [s for s in lst["payload"]["sessions"] if s["id"] == sid]
        assert sess and sess[0]["attached"] is True


async def test_task_send_hitl_roundtrip_and_close(server):
    _, port = server
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await _handshake(ws)
        await _new_session(ws)
        await ws.send(make_message(MsgType.TASK_SEND, {"text": "hi"}))
        msgs = await _drive(ws, answer="a")
        types = [m["type"] for m in msgs]
        assert MsgType.ASK.value in types
        assert MsgType.CLOSE.value in types
        # 实时流包含瞬时 tool_call_delta（证明瞬时事件也转发）
        assert MsgType.EVENT.value in types
        # FINAL 事件内容为 ask 应答回填
        final = next(
            m for m in msgs
            if m["type"] == MsgType.EVENT.value and m["payload"]["event"]["type"] == EventType.FINAL.value
        )
        assert final["payload"]["event"]["text"] == "ans=a"


async def test_replay_excludes_transient(server):
    _, port = server
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await _handshake(ws)
        sid = await _new_session(ws)
        await ws.send(make_message(MsgType.TASK_SEND, {"text": "hi"}))
        await _drive(ws, answer="a")  # 跑完一轮，缓冲落 [TEXT, FINAL]
        # 切换会话再切回，触发回放
        await ws.send(make_message(MsgType.SESSION_DETACH))
        await _recv_type(ws, MsgType.DETACHED)
        await ws.send(make_message(MsgType.SESSION_ATTACH, {"session_id": sid}))
        await _recv_type(ws, MsgType.ATTACHED)
        # 收集 replay_start..replay_end
        start = await _recv_type(ws, MsgType.REPLAY_START)
        assert start
        replayed = []
        while True:
            raw = await asyncio.wait_for(ws.recv(), 3.0)
            d = parse_message(raw)
            if d["type"] == MsgType.REPLAY_END.value:
                break
            if d["type"] == MsgType.EVENT.value:
                replayed.append(d["payload"]["event"]["type"])
        # 回放仅含持久化事件：TEXT + FINAL，且不含 TOOL_CALL_DELTA
        assert EventType.TEXT.value in replayed
        assert EventType.FINAL.value in replayed
        assert EventType.TOOL_CALL_DELTA.value not in replayed


async def test_concurrent_task_send_returns_busy(server):
    _, port = server
    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await _handshake(ws)
        await _new_session(ws)
        await ws.send(make_message(MsgType.TASK_SEND, {"text": "hi"}))
        # 立即再发一条（此时 step 在 await ask，busy）
        await ws.send(make_message(MsgType.TASK_SEND, {"text": "again"}))
        # 收集：应包含 ERROR(busy) 与后续正常完成的 CLOSE
        saw_busy = False
        got_close = False
        while not got_close:
            raw = await asyncio.wait_for(ws.recv(), 3.0)
            d = parse_message(raw)
            if d["type"] == MsgType.ERROR.value and d["payload"].get("code") == "busy":
                saw_busy = True
            elif d["type"] == MsgType.ASK.value:
                await ws.send(make_message(MsgType.ANSWER, {"id": d["id"], "text": "a"}, id=d["id"]))
            elif d["type"] == MsgType.CLOSE.value:
                got_close = True
        assert saw_busy and got_close


def test_attach_restores_from_factory_and_seeds_buffer():
    """M6.2 冷启动：attach 到内存不存在但 store 中存在的 id 时，经 restore_factory 恢复并播种回放缓冲。"""
    stream = EventStream()
    stream.append(Event(type=EventType.USER, text="hi"))
    stream.append(Event(type=EventType.DECISION, decision=Decision(text="ok")))
    fake = SimpleNamespace(session_id="x", event_stream=stream)
    reg = SessionRegistry(restore_factory=lambda sid: fake if sid == "x" else None)

    class Conn:
        def __init__(self):
            self.session_id = None

        async def send(self, *a, **k):
            return None

    c = Conn()
    h = reg.attach(c, "x")
    assert h is not None
    assert h.session_id == "x"
    # 回放缓冲已播种最近事件（USER + DECISION）
    assert [e.type for e in h.event_buffer] == [EventType.USER, EventType.DECISION]
