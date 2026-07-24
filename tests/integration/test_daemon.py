"""M7 daemon 测试：SessionRegistry 单元 + WebSocket 端到端（BridgeTransport + HITL + 回放 + busy）。

使用真实 websockets 在临时端口起服务，配合 FakeSession（经 BridgeTransport 驱动 HITL future）。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest
import websockets

from agent.config.settings import load_settings
from agent.context.session_store import SessionStore
from agent.core.events import Event, EventStream, EventType
from agent.core.intent import Question
from agent.core.loop import AgentResult
from agent.core.model import Decision
from agent.daemon.bridge import BridgeTransport
from agent.daemon.protocol import MsgType, make_message, parse_message
from agent.daemon.registry import SessionRegistry
from agent.daemon.server import create_ws_server
from agent.obs.store import TraceStore
from agent.obs.tracer import Tracer


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
        stream.emit(
            Event(type=EventType.TOOL_CALL_DELTA, tc_index=0, tc_name="write", tc_args='{"x":')
        )
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
        session_factory=lambda pr, sid: FakeSession(sid),
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
            await ws.send(
                make_message(MsgType.CONFIRM_PLAN, {"id": d["id"], "confirmed": True}, id=d["id"])
            )
        elif d["type"] == MsgType.APPROVE.value:
            await ws.send(
                make_message(MsgType.APPROVE, {"id": d["id"], "approved": True}, id=d["id"])
            )
        elif d["type"] == MsgType.CLOSE.value:
            break
    return collected


# --------------------------------------------------------------------------- #
# SessionRegistry 单元
# --------------------------------------------------------------------------- #
def test_registry_attach_switch_list():
    reg = SessionRegistry()
    h1 = reg.new(os.getcwd(), name="a")
    h2 = reg.new(os.getcwd(), name="b")

    class Conn:
        def __init__(self):
            self.session_id = None

        async def send(self, *a, **k):
            return None

    c = Conn()
    assert reg.attach(c, os.getcwd(), h1.session_id) is h1
    assert h1.attached_conn is c
    reg.switch(c, os.getcwd(), h2.session_id)
    assert h2.attached_conn is c
    assert h1.attached_conn is None
    info = reg.list_info()
    assert len(info) == 2
    assert {i["id"] for i in info} == {h1.session_id, h2.session_id}
    assert reg.detach(c) == h2.session_id
    assert h2.attached_conn is None


async def test_per_session_lock_serializes():
    reg = SessionRegistry()
    h = reg.new(os.getcwd())
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
            m
            for m in msgs
            if m["type"] == MsgType.EVENT.value
            and m["payload"]["event"]["type"] == EventType.FINAL.value
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
                await ws.send(
                    make_message(MsgType.ANSWER, {"id": d["id"], "text": "a"}, id=d["id"])
                )
            elif d["type"] == MsgType.CLOSE.value:
                got_close = True
        assert saw_busy and got_close


def test_attach_restores_from_factory_and_seeds_buffer():
    """M6.2 冷启动：attach 到内存不存在但 store 中存在的 id 时，经 restore_factory 恢复并播种回放缓冲。"""
    stream = EventStream()
    stream.append(Event(type=EventType.USER, text="hi"))
    stream.append(Event(type=EventType.DECISION, decision=Decision(text="ok")))
    fake = SimpleNamespace(session_id="x", event_stream=stream)
    reg = SessionRegistry(restore_factory=lambda pr, sid: fake if sid == "x" else None)

    class Conn:
        def __init__(self):
            self.session_id = None

        async def send(self, *a, **k):
            return None

    c = Conn()
    h = reg.attach(c, os.getcwd(), "x")
    assert h is not None
    assert h.session_id == "x"
    # 回放缓冲已播种最近事件（USER + DECISION）
    assert [e.type for e in h.event_buffer] == [EventType.USER, EventType.DECISION]


# --------------------------------------------------------------------------- #
# M9.0 多项目感知
# --------------------------------------------------------------------------- #
def _tmp_project() -> str:
    return tempfile.mkdtemp(prefix="m9proj-")


def test_registry_isolates_projects():
    """两个不同 project_root 下 new 同名会话：list_info(pr) 各自隔离，sqlite 落在各自 .agent。"""

    stores: dict[str, SessionStore] = {}

    def store_factory(pr: str) -> SessionStore:
        if pr not in stores:
            db = os.path.join(pr, ".agent", "sessions", "sessions.db")
            stores[pr] = SessionStore(db)
        return stores[pr]

    reg = SessionRegistry(
        session_factory=lambda pr, sid: FakeSession(sid),
        transport_factory=lambda h: BridgeTransport(h),
        store_factory=store_factory,
    )
    pr_a, pr_b = _tmp_project(), _tmp_project()
    ha = reg.new(pr_a, name="same")
    hb = reg.new(pr_b, name="same")  # 同名、不同项目

    list_a = reg.list_info(pr_a)
    list_b = reg.list_info(pr_b)
    assert [s["id"] for s in list_a] == [ha.session_id]
    assert [s["id"] for s in list_b] == [hb.session_id]
    # sqlite 文件落在各自项目根
    assert os.path.isfile(os.path.join(pr_a, ".agent", "sessions", "sessions.db"))
    assert os.path.isfile(os.path.join(pr_b, ".agent", "sessions", "sessions.db"))
    # 无 project_root 时返回全部内存会话
    assert len(reg.list_info()) == 2


def test_list_info_merges_persisted_sessions():
    """list_info(pr) 合并该项目已持久化但不在内存的会话（按 id 去重）。"""
    pr = _tmp_project()
    store = SessionStore(os.path.join(pr, ".agent", "sessions", "sessions.db"))
    store.create("persisted-1", name="from-disk")

    reg = SessionRegistry(
        session_factory=lambda p, sid: FakeSession(sid),
        transport_factory=lambda h: BridgeTransport(h),
        store_factory=lambda p: store,
    )
    h = reg.new(pr, name="in-memory")
    infos = reg.list_info(pr)
    ids = {i["id"] for i in infos}
    assert ids == {"persisted-1", h.session_id}
    assert "persisted-1" in ids
    assert len([i for i in infos if i["id"] == "persisted-1"]) == 1  # 去重


@pytest.fixture
async def multi_project_server():
    """带真实 per-project SessionStore 的 daemon（drives 用 FakeSession）。"""
    stores: dict[str, SessionStore] = {}

    def store_factory(pr: str) -> SessionStore:
        if pr not in stores:
            stores[pr] = SessionStore(os.path.join(pr, ".agent", "sessions", "sessions.db"))
        return stores[pr]

    def session_factory(pr: str, sid: str) -> FakeSession:
        store_factory(pr).create(sid, name=sid[:8])  # 在该项目 store 登记
        return FakeSession(sid)

    registry = SessionRegistry(
        session_factory=session_factory,
        transport_factory=lambda h: BridgeTransport(h),
        store_factory=store_factory,
    )
    srv = await create_ws_server(registry, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    yield {
        "registry": registry,
        "store_factory": store_factory,
        "port": port,
        "pr_a": _tmp_project(),
        "pr_b": _tmp_project(),
    }
    srv.close()
    await srv.wait_closed()


async def test_multi_project_session_isolation(multi_project_server):
    """同一 daemon 服务两个项目：会话 Event 流与 SessionStore 完全隔离、互不串扰。"""
    srv = multi_project_server

    async with websockets.connect(f"ws://127.0.0.1:{srv['port']}") as ws:
        await _handshake(ws)
        await ws.send(make_message(MsgType.SESSION_NEW, {"project_root": srv["pr_a"]}))
        created_a = await _recv_type(ws, MsgType.SESSION_CREATED)
        sid_a = created_a["payload"]["session_id"]
        assert created_a["payload"]["project_root"] == srv["pr_a"]
        await _recv_type(ws, MsgType.ATTACHED)
        await ws.send(make_message(MsgType.TASK_SEND, {"text": "hi"}))
        msgs_a = await _drive(ws, answer="a")
        finals_a = [
            m
            for m in msgs_a
            if m["type"] == MsgType.EVENT.value
            and m["payload"]["event"]["type"] == EventType.FINAL.value
        ]
        assert len(finals_a) == 1
        assert finals_a[0]["payload"]["event"]["text"] == "ans=a"

    async with websockets.connect(f"ws://127.0.0.1:{srv['port']}") as ws2:
        await _handshake(ws2)
        await ws2.send(make_message(MsgType.SESSION_NEW, {"project_root": srv["pr_b"]}))
        created_b = await _recv_type(ws2, MsgType.SESSION_CREATED)
        sid_b = created_b["payload"]["session_id"]
        await _recv_type(ws2, MsgType.ATTACHED)
        await ws2.send(make_message(MsgType.TASK_SEND, {"text": "hi"}))
        await _drive(ws2, answer="b")

    # 两个项目的 SessionStore 完全隔离
    ids_a = {r["session_id"] for r in srv["store_factory"](srv["pr_a"]).list_sessions()}
    ids_b = {r["session_id"] for r in srv["store_factory"](srv["pr_b"]).list_sessions()}
    assert sid_a in ids_a and sid_a not in ids_b
    assert sid_b in ids_b and sid_b not in ids_a
    # 协议层列表也按项目隔离
    assert {s["id"] for s in srv["registry"].list_info(srv["pr_a"])} == {sid_a}
    assert {s["id"] for s in srv["registry"].list_info(srv["pr_b"])} == {sid_b}


# --------------------------------------------------------------------------- #
# M9.7 可观测面板：trace 查询协议
# --------------------------------------------------------------------------- #
@pytest.fixture
async def trace_server():
    """带真实 per-project TraceStore 的 daemon（drives 用 FakeSession）。"""

    def trace_store_factory(pr: str) -> TraceStore:
        return TraceStore(os.path.join(pr, ".agent", "traces.db"))

    registry = SessionRegistry(
        session_factory=lambda pr, sid: FakeSession(sid),
        transport_factory=lambda h: BridgeTransport(h),
        trace_store_factory=trace_store_factory,
    )
    srv = await create_ws_server(registry, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    yield {"registry": registry, "trace_store_factory": trace_store_factory, "port": port, "pr": _tmp_project()}
    srv.close()
    await srv.wait_closed()


async def test_trace_list_and_get(trace_server):
    """trace.list 列出会话摘要；trace.get 返回 span 树（含父子 parent_id）。"""
    srv = trace_server
    ts = srv["trace_store_factory"](srv["pr"])
    tracer = Tracer(session_id="trace-sess-1")
    with tracer.span("agent.run", kind="agent") as parent:
        parent.log("task", "demo")
        with tracer.span("model.act", kind="model", parent=parent):
            pass
    ts.save_trace(tracer)

    async with websockets.connect(f"ws://127.0.0.1:{srv['port']}") as ws:
        await _handshake(ws)
        # trace.list（带 id 配对）
        rid = "req-1"
        await ws.send(make_message(MsgType.TRACE_LIST, {"project_root": srv["pr"]}, id=rid))
        resp = await _recv_type(ws, MsgType.TRACE_LIST_RESP)
        assert resp["id"] == rid
        assert resp["payload"]["project_root"] == srv["pr"]
        traces = resp["payload"]["traces"]
        assert len(traces) == 1
        assert traces[0]["session_id"] == "trace-sess-1"
        assert traces[0]["span_count"] == 2

        # trace.get 单条
        rid2 = "req-2"
        await ws.send(
            make_message(
                MsgType.TRACE_GET,
                {"project_root": srv["pr"], "trace_id": "trace-sess-1"},
                id=rid2,
            )
        )
        tree = await _recv_type(ws, MsgType.TRACE_TREE)
        assert tree["id"] == rid2
        spans = tree["payload"]["spans"]
        assert len(spans) == 2
        root = next(s for s in spans if s["parent_id"] is None)
        assert root["name"] == "agent.run"
        assert root["status"] == "ok"
        assert root["logs"][0]["key"] == "task"
        child = next(s for s in spans if s["parent_id"] is not None)
        assert child["parent_id"] == root["span_id"]


async def test_trace_filter_and_empty(trace_server):
    """trace.list 按 session_id 过滤；trace.get 命中空返回空 spans（不报错）。"""
    srv = trace_server
    ts = srv["trace_store_factory"](srv["pr"])
    for name in ("sess-x", "sess-y"):
        t = Tracer(session_id=name)
        with t.span(f"run-{name}"):
            pass
        ts.save_trace(t)

    async with websockets.connect(f"ws://127.0.0.1:{srv['port']}") as ws:
        await _handshake(ws)
        await ws.send(
            make_message(MsgType.TRACE_LIST, {"project_root": srv["pr"], "session_id": "sess-x"})
        )
        resp = await _recv_type(ws, MsgType.TRACE_LIST_RESP)
        assert len(resp["payload"]["traces"]) == 1
        assert resp["payload"]["traces"][0]["session_id"] == "sess-x"

        await ws.send(
            make_message(MsgType.TRACE_GET, {"project_root": srv["pr"], "trace_id": "nope"})
        )
        tree = await _recv_type(ws, MsgType.TRACE_TREE)
        assert tree["payload"]["spans"] == []


async def test_trace_isolation_across_projects(trace_server):
    """不同 project_root 的 trace 互不串扰：pr_b 看不到 pr_a 的 trace。"""
    srv = trace_server
    ts_a = srv["trace_store_factory"](srv["pr"])
    t = Tracer(session_id="only-in-a")
    with t.span("run"):
        pass
    ts_a.save_trace(t)

    pr_b = _tmp_project()
    async with websockets.connect(f"ws://127.0.0.1:{srv['port']}") as ws:
        await _handshake(ws)
        await ws.send(make_message(MsgType.TRACE_LIST, {"project_root": srv["pr"]}))
        a_traces = (await _recv_type(ws, MsgType.TRACE_LIST_RESP))["payload"]["traces"]
        assert any(t["session_id"] == "only-in-a" for t in a_traces)

        await ws.send(make_message(MsgType.TRACE_LIST, {"project_root": pr_b}))
        b_traces = (await _recv_type(ws, MsgType.TRACE_LIST_RESP))["payload"]["traces"]
        assert all(t["session_id"] != "only-in-a" for t in b_traces)
        assert b_traces == []
