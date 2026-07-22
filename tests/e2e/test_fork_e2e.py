"""M6.5 e2e（slow）：fork 跨 daemon 重启端到端验收。

模拟 daemon 重启 = 丢弃内存态，从同一 sqlite ``SessionStore`` 重新加载。验证：
- fork 子会话 events 含父前缀、独立分支、互不串台；
- ``list_sessions`` 显示 ``parent_session_id`` 血缘；
- 父/子各自 resume 成功并续跑，互不影响。

默认 ``pytest -q`` 跳过（addopts=-m 'not slow'），``pytest -m slow`` 单独跑。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from agent.context.session_store import SessionStore
from agent.core.events import EventType
from agent.core.model import Decision, FakeModel
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
def test_fork_across_daemon_restart_lineage(tmp_path):
    settings = _settings()
    _disable_compaction(settings)
    db = tmp_path / "sessions.db"

    store = SessionStore(db)
    parent = uuid.uuid4().hex
    store.create(parent)
    parent_session = Session(
        FakeModel([Decision(text="parent answer")]),
        default_registry,
        settings,
        tracer=None,
        session_id=parent,
        session_store=store,
    )
    asyncio.run(parent_session.step("parent task", TerminalTransport(interactive=False)))
    parent_messages = list(parent_session.messages)
    parent_prefix = store.load(parent).all()

    # fork 派生子分支
    child = store.fork(parent, name="branch")

    # 模拟 daemon 重启：父子各自从同一 store 重新加载
    restarted_store = SessionStore(db)
    restored_parent = Session.from_store(
        FakeModel([Decision(text="p2")]), default_registry, settings, restarted_store, parent
    )
    restored_child = Session.from_store(
        FakeModel([Decision(text="c2")]), default_registry, settings, restarted_store, child
    )

    # 子消息以父前缀开头（fork 复制语义）
    assert restored_child.messages[: len(parent_messages)] == parent_messages

    # list_sessions 血缘可见
    sessions = restarted_store.list_sessions()
    by_id = {s["session_id"]: s for s in sessions}
    assert by_id[child]["parent_session_id"] == parent

    # 各自续跑
    asyncio.run(restored_parent.step("continue parent", TerminalTransport(interactive=False)))
    asyncio.run(restored_child.step("continue child", TerminalTransport(interactive=False)))

    parent_after = restarted_store.load(parent).all()
    child_after = restarted_store.load(child).all()

    # 子事件流以父前缀（fork 时复制）开头
    assert child_after[: len(parent_prefix)] == parent_prefix
    # 父续跑新增、子续跑新增均非空
    parent_new = parent_after[len(parent_prefix):]
    child_new = child_after[len(parent_prefix):]
    assert parent_new and child_new
    # 互不串台：父续跑的最终答案不在子流，子续跑的最终答案不在父流
    assert all(e.type != EventType.FINAL or e.text != "p2" for e in child_new)
    assert all(e.type != EventType.FINAL or e.text != "c2" for e in parent_new)
