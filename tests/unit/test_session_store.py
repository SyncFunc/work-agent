"""M6.1 SessionStore 测试：跨重启持久化 + fork 血缘 + sink 零侵入 + 与 Session 集成。

不依赖真实 LLM：用合成 EventStream 与 FakeModel 验证存储层与接入点。
"""

from __future__ import annotations

import asyncio
import uuid

from agent.context.session_store import (
    TITLE_SOURCE_MEMORY,
    TITLE_SOURCE_USER,
    SessionStore,
    SessionStoreSink,
)
from agent.core.events import Event, EventStream, EventType
from agent.core.model import Decision, FakeModel, ToolCall
from agent.core.session import Session
from agent.runtime.registry import ToolResult, default_registry


def _populate(es: EventStream) -> None:
    """向给定 EventStream 追加一组代表性事件（含瞬时事件）。"""
    es.append(Event(type=EventType.DECISION, decision=Decision(text="think", tool_calls=[])))
    es.append(
        Event(
            type=EventType.TOOL_USE,
            tool_use=ToolCall(id="c1", name="read", arguments={"path": "a.txt"}),
        )
    )
    es.append(
        Event(
            type=EventType.TOOL_RESULT,
            tool_call_id="c1",
            tool_result=ToolResult(ok=True, output="hi"),
        )
    )
    es.append(Event(type=EventType.FINAL, text="done"))
    # 瞬时事件：不持久化
    es.emit(Event(type=EventType.TOOL_CALL_DELTA, tc_index=0, tc_name="read", tc_args='{"path":'))


def test_roundtrip_persist_excludes_transient(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    store.create("s1")
    es = EventStream()
    es.subscribe(SessionStoreSink(store, "s1"))  # 模拟 loop.run 订阅持久化 sink
    _populate(es)

    loaded = store.load("s1")
    assert loaded is not None
    types = [e.type for e in loaded.all()]
    assert EventType.TOOL_CALL_DELTA not in types  # 瞬时排除
    assert types == [EventType.DECISION, EventType.TOOL_USE, EventType.TOOL_RESULT, EventType.FINAL]
    # seq 保持一致
    assert [e.seq for e in loaded.all()] == [0, 1, 2, 3]


def test_fork_copies_parent_prefix(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    store.create("parent")
    es = EventStream()
    _populate(es)
    store.append_events("parent", es)

    child = store.fork("parent", name="branch")
    assert store.get_parent(child) == "parent"
    parent_loaded = store.load("parent")
    child_loaded = store.load(child)
    assert [e.type for e in child_loaded.all()] == [e.type for e in parent_loaded.all()]
    # 子会话独立演进：追加不影响父
    child_loaded.append(Event(type=EventType.FINAL, text="child-only"))
    store.append_event(child, child_loaded.all()[-1])
    assert len(store.load("parent").all()) == 4
    assert len(store.load(child).all()) == 5


def test_list_sessions_includes_parent_link(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    store.create("a", name="root")
    b = store.fork("a", name="branch")
    infos = store.list_sessions()
    by_id = {i["session_id"]: i for i in infos}
    assert by_id["a"]["parent_session_id"] is None
    assert by_id[b]["parent_session_id"] == "a"


def test_session_step_persists_via_sink(tmp_path, monkeypatch):
    """集成：Session.step 通过 event_sink 把本轮 events 落盘。"""
    # 避免 SessionMemory / 压缩在测试中落盘到仓库
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
    from agent.runtime.terminal_transport import TerminalTransport

    transport = TerminalTransport(interactive=False)
    res, _ = asyncio.run(session.step("say hi", transport))
    assert res.text

    loaded = store.load(sid)
    assert loaded is not None
    assert EventType.FINAL in [e.type for e in loaded.all()]
    assert store.get_session(sid) is not None


def test_iter_events_with_subsession_groups_parent_and_children(tmp_path):
    """M9 subsession：iter_events_with_subsession 返回父事件（subsession_id=None）+

    子事件（subsession_id=子会话 id），按 ts 升序，供回放重建主聊天独立块历史。
    """
    store = SessionStore(tmp_path / "sessions.db")
    store.create("parent")

    # 父会话事件
    p_es = EventStream()
    p_es.append(Event(type=EventType.DECISION, ts=1.0, decision=Decision(text="p1", tool_calls=[])))
    store.append_events("parent", p_es)

    # 子会话事件（带 parent_session_id）
    child_id = "parent/sub_general_0_abc123"
    c_es = EventStream()
    c_es.append(Event(type=EventType.TEXT, ts=2.0, text="sub work", kind="content"))
    c_es.append(Event(type=EventType.FINAL, ts=3.0, text="sub done"))
    for ev in c_es.all():
        store.append_event(child_id, ev, parent_session_id="parent")

    rows = store.iter_events_with_subsession("parent")
    subs = [(ev.type.value, sub) for ev, sub in rows]
    # 父在前、子在后（按 ts）
    assert subs[0] == ("decision", None)
    assert subs[1] == ("text", child_id)
    assert subs[2] == ("final", child_id)
    # 子事件确已带 parent_session_id 落盘
    assert store.get_parent("parent") is None  # 父自身无父
    # 通过回放条件验证：子事件可由 parent 查到
    child_rows = [r for r in rows if r[1] == child_id]
    assert len(child_rows) == 2


def test_title_set_and_resolve_persists(tmp_path):
    """M11.6 标题持久化：set_title 后跨「重启」（新 store 实例）仍能解析。"""
    store = SessionStore(tmp_path / "sessions.db")
    store.create("s1")
    store.set_title("s1", "第一个提问：如何优化", TITLE_SOURCE_USER)
    assert store.resolve_title("s1") == "第一个提问：如何优化"
    # 模拟重启：同一 db 路径新建 store 实例
    store2 = SessionStore(tmp_path / "sessions.db")
    assert store2.resolve_title("s1") == "第一个提问：如何优化"
    row = store2.get_session("s1")
    assert row["title_source"] == TITLE_SOURCE_USER


def test_title_source_migration_on_existing_db(tmp_path):
    """旧库（无 title/title_source 列）在线迁移后仍可正常读写标题。"""
    import sqlite3

    db = tmp_path / "sessions.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY, name TEXT, parent_session_id TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, plan_mode INTEGER,
                plan_path TEXT, clarify_total INTEGER, root_span_id TEXT, model_meta_json TEXT)"""
        )
        conn.execute(
            "INSERT INTO sessions (session_id, created_at, updated_at) VALUES ('old', 0, 0)"
        )
    store = SessionStore(db)  # 触发 ALTER 迁移
    store.set_title("old", "迁移后标题", TITLE_SOURCE_MEMORY)
    assert store.resolve_title("old") == "迁移后标题"
