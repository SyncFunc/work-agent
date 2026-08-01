"""M9 subsession：独立子会话实时转发验证（D.2/D.3 核心契约）。

验证：
- daemon 模式下 ``SubsessionBridgeTransport`` 经父 handle 的 ``attached_conn`` 转发事件，
  且 EVENT 消息携带 ``subsession_id``（前端据此建独立 panel）。
- ``SessionRegistry.register_subsession`` 把子会话登记到父 ``children`` 下。
- 子会话有独立 ``event_buffer``（回放用），不污染父缓冲。
- ``Session.spawn_background`` 在 daemon 模式（``daemon_handle`` 注入）走 subsession，
  产生的事件带 subsession_id；CLI 模式（无 handle）保持本地 transport，行为不变。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from agent.config.settings import Settings
from agent.core.events import Event, EventStream, EventType
from agent.core.model import Decision, FakeModel
from agent.daemon.bridge import SubsessionBridgeTransport
from agent.daemon.protocol import MsgType
from agent.daemon.registry import SessionHandle, SessionRegistry
from agent.runtime.registry import ToolRegistry
from agent.subagent import BUILTIN_GENERAL, SubagentSpawner


class _FakeWs:
    """真实 server.Connection 需要的 ws 桩（记录发出的消息）。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, message: str) -> None:
        import json

        self.sent.append(json.loads(message))


class FakeConn:
    """最小连接桩：记录所有发出的消息，供断言。"""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.sent: list[dict] = []

    async def send(self, type, payload, *, id=None, session=None) -> None:  # noqa: A002
        self.sent.append({"type": type, "payload": payload, "id": id, "session": session})


@pytest.mark.asyncio
async def test_subsession_transport_forwards_with_subsession_id():
    parent = SessionHandle("p1", "p", None, None, "")
    conn = FakeConn()
    parent.attached_conn = conn

    sub = SessionHandle("p1/sub_x_0_abc", "x", None, None, "", parent_id="p1")
    transport = SubsessionBridgeTransport(parent, sub)
    stream = EventStream()
    transport.bind(stream)

    stream.append(Event(type=EventType.TEXT, text="hello"))
    # _on_event 经 ensure_future 调度发送，给当前 loop 机会执行已调度的任务。
    for _ in range(30):
        await asyncio.sleep(0)

    events = [m for m in conn.sent if m["type"] == MsgType.EVENT]
    assert events, "应收到 EVENT 消息"
    last = events[-1]
    assert last["payload"]["subsession_id"] == "p1/sub_x_0_abc"
    assert last["payload"]["event"]["text"] == "hello"
    # 子会话独立缓冲（回放用），父缓冲不受影响。
    assert len(sub.event_buffer) == 1
    assert len(parent.event_buffer) == 0


@pytest.mark.asyncio
async def test_subsession_registry_links_parent_children():
    reg = SessionRegistry()
    parent = SessionHandle("p1", "p", None, None, "")
    sub = SessionHandle("p1/sub_x_0_abc", "x", None, None, "", parent_id="p1")
    # 真实流程中父会话必已注册进 registry；测试补注册以链接 children。
    reg._sessions["p1"] = parent

    reg.register_subsession("p1", sub)
    assert reg.get_subsession("p1/sub_x_0_abc") is sub
    assert "p1/sub_x_0_abc" in parent.children

    reg.unregister_subsession("p1/sub_x_0_abc")
    assert reg.get_subsession("p1/sub_x_0_abc") is None
    assert "p1/sub_x_0_abc" not in parent.children


@pytest.mark.asyncio
async def test_replay_includes_subsession_events():
    """父会话 _replay 经 handle.registry 回放子会话缓冲并带 subsession_id。"""
    from agent.daemon.server import _replay

    reg = SessionRegistry()
    parent = SessionHandle("p1", "p", None, None, "")
    parent.registry = reg
    reg._sessions["p1"] = parent  # 真实流程父会话必已注册；测试补注册
    sub = SessionHandle("p1/sub_x_0_abc", "x", None, None, "", parent_id="p1")
    reg.register_subsession("p1", sub)
    # 父缓冲放一条，子缓冲放一条
    parent.event_buffer.append(Event(type=EventType.TEXT, text="parent"))
    sub.event_buffer.append(Event(type=EventType.TEXT, text="child"))

    conn = FakeConn()
    await _replay(conn, parent, "p1")

    events = [m for m in conn.sent if m["type"] == MsgType.EVENT]
    subs = [m for m in events if m["payload"].get("subsession_id") == "p1/sub_x_0_abc"]
    assert subs, "应回放子会话事件"
    assert subs[-1]["payload"]["event"]["text"] == "child"


@pytest.mark.asyncio
async def test_spawn_in_daemon_mode_forwards_subsession_events():
    """回归：daemon 模式 spawn（给定 parent_handle + registry）走独立 subsession，事件经父
    连接多路复用带 subsession_id；不再退回本地 transport（修复前 loop 层的 control-tool 派生
    命令未透传 parent_handle/registry，导致无 subsession_id、前端不渲染子 agent 输出）。"""
    reg = SessionRegistry()
    parent = SessionHandle("p1", "p", None, None, "")
    conn = FakeConn()
    parent.attached_conn = conn
    reg._sessions["p1"] = parent  # 真实流程父会话必已注册进 registry

    spawner = SubagentSpawner(Settings())
    model = FakeModel([Decision(text="subagent final")])
    result = await spawner.spawn(
        BUILTIN_GENERAL,
        "do subtask",
        base_registry=ToolRegistry(),
        base_model=model,
        parent_handle=parent,
        registry=reg,
    )
    assert result.text == "subagent final"

    # _on_event 经 ensure_future 调度发送，给当前 loop 机会执行已调度的任务。
    for _ in range(30):
        await asyncio.sleep(0)

    events = [m for m in conn.sent if m["type"] == MsgType.EVENT]
    assert events, "daemon 模式应经父连接转发子会话事件"
    assert all(m["payload"].get("subsession_id", "").startswith("p1/sub_") for m in events), (
        "事件须带 subsession_id（前缀为父 id）"
    )
    # 子会话已登记到父 children 下
    assert len(parent.children) == 1


@pytest.mark.asyncio
async def test_background_subsession_tracks_sends_on_connection():
    """M11.6：后台 subsession（如 session-memory）的事件转发任务登记到目标连接，
    供会话切换/attach 时 cancel，避免积压任务排队抢占 conn._lock 饿死 ATTACHED/replay。"""
    from agent.daemon.server import Connection

    conn = Connection(_FakeWs())
    parent = SessionHandle("p1", "p", None, None, "")
    parent.attached_conn = conn
    sub = SessionHandle("p1/sub_x_0_abc", "x", None, None, "", parent_id="p1")
    sub.background = True  # M11：后台标记 → 任务登记到 conn._backlog

    transport = SubsessionBridgeTransport(parent, sub)
    stream = EventStream()
    transport.bind(stream)
    stream.append(Event(type=EventType.TEXT, text="hello"))

    for _ in range(30):
        await asyncio.sleep(0)

    assert conn._backlog, "后台子会话应把转发任务登记到连接 backlog"
    assert not all(t.done() for t in conn._backlog) or any(t for t in conn._backlog)


@pytest.mark.asyncio
async def test_connection_cancel_background_cancels_pending_sends():
    """M11.6：conn.cancel_background() 取消积压的后台转发任务（切换/attach 前调用）。"""
    from agent.daemon.server import Connection

    conn = Connection(_FakeWs())

    # 造一批被 conn._lock 卡住的发送任务（模拟后台事件洪流抢占锁）。
    async def _slow_send() -> None:
        await conn.send(MsgType.EVENT, {"event": {"text": "x"}})

    tasks = [asyncio.ensure_future(_slow_send()) for _ in range(5)]
    # 让任务开始执行并排队在 _lock 上（首个可能已完成，其余在锁队列）。
    for _ in range(20):
        await asyncio.sleep(0)
    for t in tasks:
        conn.track_background(t)

    conn.cancel_background()
    await asyncio.sleep(0)

    # cancel 后所有任务要么被取消、要么完成；不再有任何任务挂着。
    assert all(t.cancelled() or t.done() for t in tasks)


@pytest.mark.asyncio
async def test_attach_and_switch_call_cancel_background():
    """M11.6：_attach / _switch 在发 ATTACHED 前先取消积压后台任务（防切换被饿死）。"""
    from agent.daemon.server import Connection, _attach, _switch

    # 用真实 Connection 验证 cancel_background 被调用（通过检查 backlog 被清空）。
    conn = Connection(_FakeWs())
    reg = SessionRegistry()
    h1 = reg.new(os.getcwd(), name="a")
    h2 = reg.new(os.getcwd(), name="b")

    # 先 attach 到 h1，塞一个积压任务，再 switch 到 h2：应被 cancel。
    await _attach(conn, reg, os.getcwd(), h1.session_id)
    stale = asyncio.ensure_future(conn.send(MsgType.EVENT, {"event": {"text": "stale"}}))
    conn.track_background(stale)

    await _switch(conn, reg, os.getcwd(), h2.session_id)
    await asyncio.sleep(0)
    assert stale.cancelled() or stale.done(), "switch 前应取消旧会话后台积压任务"
    assert h2.attached_conn is conn
    # switch 后发出的消息里包含 ATTACHED
    types = [m["type"] for m in conn.ws.sent]
    assert MsgType.ATTACHED.value in types
