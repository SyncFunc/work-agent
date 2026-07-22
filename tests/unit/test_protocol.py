"""M7 协议层测试：信封 make/parse 往返 + 全类型编解码 + Event 经 event 消息往返保真。"""

from __future__ import annotations

from agent.core.events import Event, EventType
from agent.daemon.protocol import (
    DAEMON_VERSION,
    PROTOCOL_VERSION,
    MsgType,
    make_message,
    parse_message,
)


def test_make_parse_roundtrip_all_types():
    for mt in MsgType:
        raw = make_message(mt, {"k": "v"}, id="id1", session="s1")
        d = parse_message(raw)
        assert d["type"] == mt.value
        assert d["payload"] == {"k": "v"}
        assert d["id"] == "id1"
        assert d["session"] == "s1"


def test_make_without_optional_fields():
    raw = make_message(MsgType.HELLO)
    d = parse_message(raw)
    assert d["type"] == "hello"
    assert d["payload"] == {}
    assert "id" not in d
    assert "session" not in d


def test_event_message_roundtrip():
    ev = Event(type=EventType.TEXT, text="hi", kind="content")
    raw = make_message(MsgType.EVENT, {"event": ev.to_dict()})
    d = parse_message(raw)
    back = Event.from_dict(d["payload"]["event"])
    assert back.type == EventType.TEXT
    assert back.text == "hi"
    assert back.kind == "content"


def test_transient_event_not_serialized():
    # emit 产生的瞬时事件带 transient=True，但 to_dict 不计该字段（向后兼容）。
    from agent.core.events import EventStream

    stream = EventStream()
    ev = Event(type=EventType.TOOL_CALL_DELTA, tc_index=0, tc_name="write", tc_args='{"x":')
    stream.emit(ev)
    assert ev.transient is True
    d = ev.to_dict()
    assert "transient" not in d
    back = Event.from_dict(d)
    assert back.transient is False  # 重放时不带瞬时标记


def test_protocol_versions_exposed():
    assert DAEMON_VERSION and PROTOCOL_VERSION
    # 全部 C2S / S2C 类型齐备
    values = {m.value for m in MsgType}
    for t in [
        "hello",
        "session.new",
        "session.attach",
        "session.switch",
        "session.detach",
        "session.list",
        "task.send",
        "answer",
        "confirm_plan",
        "approve",
        "command",
        "welcome",
        "session.created",
        "attached",
        "detached",
        "session_list",
        "event",
        "replay_start",
        "replay_end",
        "ask",
        "show_questions",
        "show_plan",
        "show_skills",
        "show_agents",
        "notify",
        "usage",
        "close",
        "error",
    ]:
        assert t in values
