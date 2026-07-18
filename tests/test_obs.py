"""M3.1 验收：Tracer 增强（Span.log + TraceStore 持久化 + 恢复）。"""

from pathlib import Path

from agent.obs.tracer import LogEntry, Span, Tracer
from agent.obs.store import TraceStore


def test_span_log():
    """Span.log() 追加结构化日志，render() 含日志摘要。"""
    t = Tracer()
    with t.span("test.span") as s:
        s.log("key1", "value1")
        s.log("key2", 42)
        s.log("error_key", "something went wrong", level="error")

    assert len(s.logs) == 3
    assert s.logs[0].key == "key1"
    assert s.logs[0].value == "value1"
    assert s.logs[0].level == "info"
    assert s.logs[2].level == "error"

    rendered = t.render()
    assert "test.span" in rendered
    assert "error_key" in rendered


def test_span_log_chain():
    """Span.log() 返回自身，支持链式调用。"""
    t = Tracer()
    with t.span("test.chain") as s:
        s.log("a", 1).log("b", 2)
    assert len(s.logs) == 2


def test_span_parent_child_log():
    """父子 span 各自独立记录日志。"""
    t = Tracer()
    with t.span("parent") as parent:
        parent.log("parent_only", True)
        with t.span("child", parent=parent) as child:
            child.log("child_only", True)
    parent_logs = [lg for lg in parent.logs if lg.key == "parent_only"]
    child_logs = [lg for lg in child.logs if lg.key == "child_only"]
    assert len(parent_logs) == 1
    assert len(child_logs) == 1


def test_tracer_session_id():
    """Tracer 自动分配 session_id。"""
    t1 = Tracer()
    t2 = Tracer()
    assert t1.session_id != t2.session_id
    assert len(t1.session_id) == 12


def test_tracer_custom_session_id():
    """Tracer 可传入自定义 session_id。"""
    t = Tracer(session_id="custom123")
    assert t.session_id == "custom123"


def test_trace_store_save_load(tmp_path: Path):
    """TraceStore 保存/加载往返保真（Span 属性 + logs 列表）。"""
    db = tmp_path / "test.db"
    store = TraceStore(db)

    t = Tracer()
    with t.span("agent.run", kind="agent") as parent:
        parent.log("task", "test task")
        with t.span("model.act", kind="model", parent=parent) as mspan:
            mspan.log("provider", "test-model")
            mspan.log("tool_calls", 2)
            mspan.meta["usage"] = {"total_tokens": 100}
        with t.span("tool.exec", kind="tool", parent=parent) as tool_span:
            tool_span.log("tool", "bash")
            tool_span.log("cmd", "echo hello")

    store.save_trace(t)

    # 加载并验证
    loaded = store.load_trace(t.session_id)
    assert loaded is not None
    assert len(loaded.spans) == 3
    assert loaded.session_id == t.session_id

    # 验证 span 属性
    loaded_parent = loaded.spans[0]
    assert loaded_parent.name == "agent.run"
    assert loaded_parent.kind == "agent"
    assert loaded_parent.parent_id is None
    assert len(loaded_parent.logs) == 1
    assert loaded_parent.logs[0].key == "task"

    loaded_model = loaded.spans[1]
    assert loaded_model.name == "model.act"
    assert loaded_model.parent_id == loaded_parent.id
    assert loaded_model.meta.get("usage", {}).get("total_tokens") == 100

    loaded_tool = loaded.spans[2]
    assert loaded_tool.name == "tool.exec"
    assert len(loaded_tool.logs) == 2


def test_trace_store_load_nonexistent(tmp_path: Path):
    """加载不存在的 session 返回 None。"""
    store = TraceStore(tmp_path / "empty.db")
    assert store.load_trace("no_such_session") is None


def test_trace_store_list_sessions(tmp_path: Path):
    """list_sessions 返回所有有记录的 session。"""
    db = tmp_path / "test.db"
    store = TraceStore(db)

    t1 = Tracer()
    with t1.span("run1"):
        pass
    store.save_trace(t1)

    t2 = Tracer()
    with t2.span("run2"):
        pass
    store.save_trace(t2)

    sessions = store.list_sessions()
    assert len(sessions) == 2
    sids = {s["session_id"] for s in sessions}
    assert t1.session_id in sids
    assert t2.session_id in sids


def test_trace_store_overwrite(tmp_path: Path):
    """同一 session 覆盖写后数据完整（幂等）。"""
    db = tmp_path / "test.db"
    store = TraceStore(db)

    t = Tracer()
    with t.span("first"):
        pass
    store.save_trace(t)

    # 第二次写（同一个 session_id，不同 span）
    t2 = Tracer(session_id=t.session_id)
    with t2.span("second"):
        pass
    store.save_trace(t2)

    loaded = store.load_trace(t.session_id)
    assert loaded is not None
    assert len(loaded.spans) == 1
    assert loaded.spans[0].name == "second"


def test_span_parent_child_and_render():
    """原有的父子 span 测试保持兼容。"""
    t = Tracer()
    with t.span("agent.run", kind="agent") as parent:
        with t.span("tool.exec", kind="tool", parent=parent):
            pass

    assert len(t.spans) == 2
    child = next(s for s in t.spans if s.parent_id == parent.id)
    assert child.name == "tool.exec"
    rendered = t.render()
    assert "agent.run" in rendered
    assert "tool.exec" in rendered


def test_trace_store_created_at(tmp_path: Path):
    """TraceStore 创建时自动创建目录。"""
    db = tmp_path / "sub" / "traces.db"
    store = TraceStore(db)
    assert db.exists()
    t = Tracer()
    with t.span("test"):
        pass
    store.save_trace(t)
    assert db.stat().st_size > 0
