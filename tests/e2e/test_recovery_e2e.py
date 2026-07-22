"""M6.5 e2e（slow）：resume 跨 daemon 重启端到端验收。

模拟 daemon 重启 = 丢弃内存中的 SessionRegistry/Session，从同一 sqlite
``SessionStore`` 重新加载（这正是 ``start_daemon`` 冷启动做的事）。验证：
- 恢复后 messages 与崩溃前完整重放一致；
- 中断轮（TOOL_USE 无 TOOL_RESULT，模拟崩溃）恢复后注入「继续」并可继续；
- 可续跑（resume 后 step 正常产出最终答案）。

默认 ``pytest -q`` 跳过（addopts=-m 'not slow'），``pytest -m slow`` 单独跑。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from agent.context.session_recovery import detect_interruption
from agent.context.session_store import SessionStore
from agent.core.events import Event, EventStream, EventType
from agent.core.model import Decision, FakeModel, ToolCall
from agent.core.session import Session
from agent.runtime.registry import default_registry
from agent.runtime.terminal_transport import TerminalTransport
from tests.conftest import _settings


def _disable_compaction(s: object) -> None:
    s.context.session_memory_enabled = False
    s.context.auto_compact_enabled = False
    s.context.microcompact_enabled = False
    s.obs.enabled = False


@pytest.mark.slow
def test_resume_full_session_across_daemon_restart(tmp_path):
    settings = _settings()
    _disable_compaction(settings)
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
    asyncio.run(session.step("say hi", TerminalTransport(interactive=False)))
    live_messages = list(session.messages)

    # 模拟 daemon 重启：内存态全部丢弃，仅 sqlite 持久化保留 → 从 store 重新加载
    restarted_store = SessionStore(db)
    restored = Session.from_store(
        FakeModel([Decision(text="again")]), default_registry, settings, restarted_store, sid
    )
    assert restored.messages == live_messages
    assert restored.event_stream is not None
    assert detect_interruption(restored.event_stream) is False

    # 续跑：resume 后继续一个任务，正常产出最终答案
    asyncio.run(restored.step("continue", TerminalTransport(interactive=False)))
    assert any(m.role == "assistant" and m.content == "again" for m in restored.messages)


@pytest.mark.slow
def test_resume_interrupted_turn_across_daemon_restart(tmp_path):
    """中断轮（TOOL_USE 无 TOOL_RESULT，模拟崩溃）恢复：注入「继续」并可继续完成任务。"""
    settings = _settings()
    _disable_compaction(settings)
    db = tmp_path / "sessions.db"

    store = SessionStore(db)
    sid = uuid.uuid4().hex
    store.create(sid)
    # 模拟崩溃前的部分事件流：决策调用工具，但工具结果从未落盘
    es = EventStream()
    es.append(Event(type=EventType.USER, text="do thing"))
    es.append(
        Event(
            type=EventType.DECISION,
            decision=Decision(tool_calls=[ToolCall(id="c1", name="read", arguments={})]),
        )
    )
    es.append(Event(type=EventType.TOOL_USE, tool_use=ToolCall(id="c1", name="read", arguments={})))
    store.append_events(sid, es)

    # 模拟 daemon 重启：重新加载
    restarted_store = SessionStore(db)
    restored = Session.from_store(
        FakeModel([Decision(text="recovered")]), default_registry, settings, restarted_store, sid
    )
    # 检测到中断（末轮 tool_calls 无对应 tool 结果）
    assert detect_interruption(restored.event_stream) is True
    # 重建 messages 已丢弃悬空 assistant 并注入「继续」提示
    assert restored.messages[-1].role == "user"
    assert "中断" in restored.messages[-1].content
    # 续跑不崩溃，产出最终答案
    asyncio.run(restored.step("please finish", TerminalTransport(interactive=False)))
    assert any(m.role == "assistant" and m.content == "recovered" for m in restored.messages)
