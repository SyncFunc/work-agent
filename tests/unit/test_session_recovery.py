"""M6.2 会话恢复测试：messages 重建 + 中断检测 + fork 恢复 + 跨重启 resume。

不依赖真实 LLM：合成 EventStream 与 FakeModel 验证恢复逻辑。
"""

from __future__ import annotations

import asyncio
import uuid

from agent.context.session_recovery import detect_interruption, rebuild_messages
from agent.context.session_store import SessionStore
from agent.core.events import Event, EventStream, EventType
from agent.core.model import Decision, FakeModel, Message, ToolCall
from agent.core.session import Session
from agent.runtime.registry import ToolResult, default_registry
from agent.runtime.terminal_transport import TerminalTransport


def _tool_turn_stream() -> EventStream:
    """一轮「工具调用 → 结果 → 最终答案」的完整事件序列。"""
    es = EventStream()
    es.append(Event(type=EventType.USER, text="do thing"))
    es.append(
        Event(
            type=EventType.DECISION,
            decision=Decision(
                text=None, tool_calls=[ToolCall(id="c1", name="read", arguments={"path": "a"})]
            ),
        )
    )
    es.append(
        Event(
            type=EventType.TOOL_USE,
            tool_use=ToolCall(id="c1", name="read", arguments={"path": "a"}),
        )
    )
    es.append(
        Event(
            type=EventType.TOOL_RESULT,
            tool_call_id="c1",
            tool_result=ToolResult(ok=True, output="content"),
        )
    )
    es.append(Event(type=EventType.DECISION, decision=Decision(text="done", tool_calls=[])))
    es.append(Event(type=EventType.FINAL, text="done"))
    return es


def test_rebuild_messages_mapping():
    msgs = rebuild_messages(_tool_turn_stream())
    assert [m.role for m in msgs] == ["user", "assistant", "tool", "assistant"]
    assert msgs[0] == Message(role="user", content="do thing")
    assert msgs[1].role == "assistant"
    assert msgs[1].tool_calls[0].id == "c1"
    assert msgs[2] == Message(role="tool", content="content", tool_call_id="c1")
    assert msgs[3] == Message(role="assistant", content="done")


def test_rebuild_clarify_mapping():
    """澄清闸门：DECISION 含 ask_clarification tool_calls + CLARIFY 事件 → 合成 tool 消息完成配对。"""
    es = EventStream()
    es.append(Event(type=EventType.USER, text="q"))
    es.append(
        Event(
            type=EventType.DECISION,
            decision=Decision(
                tool_calls=[
                    ToolCall(id="k1", name="ask_clarification", arguments={"question": "x"})
                ]
            ),
        )
    )
    es.append(Event(type=EventType.CLARIFY, questions=[{"question": "x", "options": []}]))
    msgs = rebuild_messages(es)
    assert msgs[0] == Message(role="user", content="q")
    assert msgs[1].tool_calls[0].name == "ask_clarification"
    assert msgs[2].role == "tool"
    assert msgs[2].tool_call_id == "k1"


def test_detect_interruption_true():
    es = EventStream()
    es.append(
        Event(
            type=EventType.DECISION,
            decision=Decision(tool_calls=[ToolCall(id="c1", name="read", arguments={})]),
        )
    )
    es.append(Event(type=EventType.TOOL_USE, tool_use=ToolCall(id="c1", name="read", arguments={})))
    # 无 TOOL_RESULT → 中断
    assert detect_interruption(es) is True


def test_detect_interruption_false_complete():
    assert detect_interruption(_tool_turn_stream()) is False


def test_rebuild_drops_dangling_on_interruption():
    """中断：末轮 tool_calls 无结果 → 丢弃悬空 assistant，注入 user 续跑提示。"""
    es = EventStream()
    es.append(Event(type=EventType.USER, text="do"))
    es.append(
        Event(
            type=EventType.DECISION,
            decision=Decision(tool_calls=[ToolCall(id="c1", name="read", arguments={})]),
        )
    )
    es.append(Event(type=EventType.TOOL_USE, tool_use=ToolCall(id="c1", name="read", arguments={})))
    # 缺少 TOOL_RESULT
    msgs = rebuild_messages(es)
    assert msgs[-1].role == "user"
    assert "中断" in msgs[-1].content
    # 绝不出现悬空 tool_calls
    assert not any(m.role == "assistant" and m.tool_calls for m in msgs)


def test_fork_recovery_rebuild_matches_parent_prefix(tmp_path):
    """fork 子会话：load→rebuild 以父前缀开头，互不串台。"""
    store = SessionStore(tmp_path / "s.db")
    store.create("parent")
    store.append_events("parent", _tool_turn_stream())
    child = store.fork("parent", name="b")

    # 显式 seq（接续父前缀 0..5），避免 append_event 的 INSERT OR REPLACE 覆盖父事件
    store.append_event(
        child, Event(type=EventType.DECISION, seq=6, decision=Decision(text="child", tool_calls=[]))
    )
    store.append_event(child, Event(type=EventType.FINAL, seq=7, text="child"))

    parent_msgs = rebuild_messages(store.load("parent"))
    child_msgs = rebuild_messages(store.load(child))
    assert len(child_msgs) >= len(parent_msgs)
    assert child_msgs[: len(parent_msgs)] == parent_msgs


def test_resume_cross_restart_rebuilds_messages(tmp_path, monkeypatch):
    """跨重启 resume：from_store 重建的 messages 与崩溃前（完整重放）一致。"""
    import agent.tools  # noqa: F401  (注册默认工具，副作用导入)
    from agent.config.settings import Settings

    settings = Settings()
    settings.context.session_memory_enabled = False
    settings.context.auto_compact_enabled = False
    settings.context.microcompact_enabled = False
    settings.obs.enabled = False

    db = tmp_path / "sessions.db"
    store = SessionStore(db)
    sid = uuid.uuid4().hex
    store.create(sid)
    session = Session(
        FakeModel([Decision(text="hello world")]),
        default_registry,
        settings,
        tracer=None,
        session_id=sid,
        session_store=store,
    )
    transport = TerminalTransport(interactive=False)
    asyncio.run(session.step("say hi", transport))
    live_messages = list(session.messages)

    # 冷启动恢复（新进程语义：新建 Session + from_store）
    restored = Session.from_store(
        FakeModel([Decision(text="again")]), default_registry, settings, store, sid
    )
    assert restored.messages == live_messages
    assert restored.event_stream is not None
    assert detect_interruption(restored.event_stream) is False
