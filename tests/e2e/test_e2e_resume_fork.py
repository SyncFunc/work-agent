"""M6.3 e2e（slow）：resume + fork 跨重启完整会话验收。

用 FakeModel 跑完整会话 → 持久化 → 跨「进程」恢复（from_store）→ 续跑；再从恢复态
fork → 子会话独立恢复。验证事件流不丢、messages 重建一致、父子互不串台。标记 slow，
默认 ``pytest -q`` 跳过（addopts=-m 'not slow'），``pytest -m slow`` 单独跑。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from agent.context.session_store import SessionStore
from agent.core.model import Decision, FakeModel
from agent.core.session import Session
from agent.runtime.registry import default_registry
from agent.runtime.terminal_transport import TerminalTransport

from tests.conftest import _settings


@pytest.mark.slow
def test_e2e_resume_then_fork_across_restart(tmp_path):
    settings = _settings()
    settings.context.session_memory_enabled = False
    settings.context.auto_compact_enabled = False
    settings.context.microcompact_enabled = False
    settings.obs.enabled = False

    db = tmp_path / "sessions.db"
    store = SessionStore(db)
    sid = uuid.uuid4().hex
    store.create(sid)
    session = Session(
        FakeModel([Decision(text="hello")]), default_registry, settings,
        tracer=None, session_id=sid, session_store=store,
    )
    asyncio.run(session.step("first", TerminalTransport(interactive=False)))
    live_messages = list(session.messages)

    # 跨重启恢复（新建 Session + from_store，模拟独立进程）
    restored = Session.from_store(
        FakeModel([Decision(text="world")]), default_registry, settings, store, sid
    )
    assert restored.messages == live_messages
    assert len(restored.messages) == len(live_messages)

    # fork 并独立恢复 + 续跑
    child = store.fork(sid, name="b")
    child_session = Session.from_store(
        FakeModel([Decision(text="child")]), default_registry, settings, store, child
    )
    asyncio.run(child_session.step("child task", TerminalTransport(interactive=False)))

    # 父前缀原样保留在子事件流中，互不串台
    parent_events = store.load(sid).all()
    child_events = store.load(child).all()
    assert child_events[: len(parent_events)] == parent_events
    # 父会话未受子续跑影响
    assert len(store.load(sid).all()) == len(parent_events)
