"""M10.1：message 模型与 EventType.USAGE 落盘/回放基础。

验证：
- ``EventStream.append`` 是唯一 ``message_id`` / ``parent_message_id`` 打标漏斗；
- ``USAGE`` 事件携带 ``usage`` / ``duration`` / ``estimated`` 并能 ``to_dict`` / ``from_dict`` 往返；
- ``loop.run`` / ``session.step`` 通过 ``message_id`` 把一次响应的所有落盘事件关联到同一 message；
- ``USAGE`` 事件不影响 ``decisions_from_eventstream``（仅取 DECISION）。
"""

from __future__ import annotations

import asyncio

from agent.config.settings import Settings
from agent.core.events import Event, EventStream, EventType
from agent.core.loop import AgentLoop
from agent.core.model import Decision, FakeModel
from agent.core.session import Session
from agent.runtime.registry import default_registry
from agent.runtime.terminal_transport import TerminalTransport
from agent.testing.recorded_model import decisions_from_eventstream


def test_eventstream_auto_tags_message_id():
    s = EventStream()
    s.current_message_id = "m1"
    ev = Event(type=EventType.TEXT, text="hi")
    s.append(ev)
    assert ev.message_id == "m1"
    # 显式标注不被覆盖
    ev2 = Event(type=EventType.TEXT, text="ho", message_id="m2")
    s.append(ev2)
    assert ev2.message_id == "m2"
    # parent_message_id 同理
    s.current_parent_message_id = "p1"
    ev3 = Event(type=EventType.TOOL_USE, tool_use=None)
    s.append(ev3)
    assert ev3.parent_message_id == "p1"


def test_usage_event_roundtrip():
    ev = Event(
        type=EventType.USAGE,
        message_id="m1",
        usage={"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10},
        duration=1.5,
        estimated=False,
    )
    d = ev.to_dict()
    assert d["type"] == "usage"
    assert d["message_id"] == "m1"
    assert d["usage"]["total_tokens"] == 10
    assert d["duration"] == 1.5
    assert "estimated" not in d  # 默认 False 不序列化（from_dict 回填为 False）

    ev2 = Event.from_dict(d)
    assert ev2.type == EventType.USAGE
    assert ev2.message_id == "m1"
    assert ev2.usage == {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10}
    assert ev2.duration == 1.5
    assert ev2.estimated is False


def test_loop_run_tags_all_events_and_result():
    loop = AgentLoop(FakeModel([Decision(text="done")]), default_registry, Settings(), tracer=None)
    result = asyncio.run(loop.run("task", message_id="mX"))
    assert result.message_id == "mX"
    types = [e.type for e in result.events.all()]
    for ev in result.events.all():
        if not ev.transient:  # transient（tool_call_delta）不经 append，不强制
            assert ev.message_id == "mX", ev
    assert EventType.DECISION in types  # 决策事件存在且已带 message_id


def test_session_step_tags_message():
    session = Session(
        FakeModel([Decision(text="hello world")]),
        default_registry,
        Settings(),
        tracer=None,
        session_id="sess-m10-1",
    )
    res, _ = asyncio.run(
        session.step("task", TerminalTransport(interactive=False), message_id="mY")
    )
    assert res.message_id == "mY"
    for ev in session.event_stream.all():
        if not ev.transient:
            assert ev.message_id == "mY", ev


def test_usage_event_not_treated_as_decision():
    s = EventStream()
    s.append(Event(type=EventType.DECISION, decision=Decision(text="x")))
    s.append(Event(type=EventType.USAGE, message_id="m1", usage={}, duration=1.0))
    ds = decisions_from_eventstream(s)
    assert len(ds) == 1
