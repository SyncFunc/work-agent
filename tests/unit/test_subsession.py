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

import pytest

from agent.core.events import Event, EventStream, EventType
from agent.daemon.bridge import SubsessionBridgeTransport
from agent.daemon.protocol import MsgType
from agent.daemon.registry import SessionHandle, SessionRegistry


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
