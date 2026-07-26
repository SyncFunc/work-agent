"""M10.2：daemon / 子 agent 透传 agent_usage 事件。

全程用 FakeModel，不依赖真实 LLM / 真实终端交互。验收覆盖：
- 验收①：顶层 message 的 USAGE 事件落盘且含 duration（``_emit_usage``）
- 验收②：子 agent USAGE 事件带正确 ``parent_message_id``（``_emit_subagent_usage`` + spawn 端到端）
- 验收③：USAGE 事件进回放缓冲并经 EVENT 消息转发（``BridgeTransport._on_event`` + ``_replay``）
"""

from __future__ import annotations

import asyncio

from agent.config.settings import Settings
from agent.core.events import Event, EventStream, EventType
from agent.core.loop import AgentResult
from agent.core.model import Decision, FakeModel
from agent.daemon.bridge import BridgeTransport
from agent.daemon.protocol import MsgType
from agent.daemon.registry import SessionHandle, SessionRegistry
from agent.daemon.server import _emit_usage, _replay
from agent.runtime.registry import ToolRegistry
from agent.subagent import BUILTIN_GENERAL, SubagentSpawner


class _FakeConn:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, mtype, payload, *, id=None, session=None):
        self.sent.append({"type": mtype, "payload": payload, "session": session})


def _registry() -> ToolRegistry:
    return ToolRegistry()


def _settings() -> Settings:
    return Settings()


def _model() -> FakeModel:
    return FakeModel([Decision(text="sub agent done", tool_calls=[])])


def _make_result(*, message_id="mid-1", usage=None, text="hello") -> AgentResult:
    return AgentResult(
        text=text,
        events=EventStream(),
        iterations=1,
        message_id=message_id,
        usage=usage or {},
    )


# --------------------------------------------------------------------------- #
# 验收①：顶层 message 的 USAGE 事件落盘且含 duration
# --------------------------------------------------------------------------- #
def test_emit_usage_top_level_persisted_with_duration():
    stream = EventStream()
    res = _make_result(message_id="mid-1", usage={"prompt_tokens": 10, "completion_tokens": 5})
    _emit_usage(stream, res, 1.5, parent_message_id=None)
    evs = [e for e in stream if e.type == EventType.USAGE]
    assert len(evs) == 1
    ev = evs[0]
    assert ev.message_id == "mid-1"
    assert ev.parent_message_id is None
    assert ev.duration == 1.5
    assert ev.usage == {"prompt_tokens": 10, "completion_tokens": 5}
    assert ev.estimated is False


def test_emit_usage_estimated_when_empty():
    stream = EventStream()
    res = _make_result(message_id="mid-2", usage={}, text="x" * 100)
    _emit_usage(stream, res, 0.3, parent_message_id=None)
    ev = [e for e in stream if e.type == EventType.USAGE][0]
    assert ev.estimated is True
    assert "estimated_tokens" in ev.usage
    assert isinstance(ev.usage["estimated_tokens"], int)


# --------------------------------------------------------------------------- #
# 验收②：子 agent USAGE 事件带正确 parent_message_id
# --------------------------------------------------------------------------- #
def test_subagent_emit_usage_parent_message_id():
    spawner = SubagentSpawner(_settings())
    stream = EventStream()
    res = _make_result(message_id="child-mid", usage={"prompt_tokens": 3})
    spawner._emit_subagent_usage(stream, res, 0.5, parent_message_id="parent-mid")
    ev = [e for e in stream if e.type == EventType.USAGE][0]
    assert ev.message_id == "child-mid"
    assert ev.parent_message_id == "parent-mid"
    assert ev.duration == 0.5


async def test_spawn_daemon_emits_usage_with_parent(monkeypatch):
    reg = SessionRegistry()
    parent = SessionHandle("parent-sid", "parent", None, "/tmp/proj")
    parent.registry = reg
    spawner = SubagentSpawner(_settings())
    captured: list[tuple[AgentResult, str | None]] = []

    def fake_emit(stream, result, duration, *, parent_message_id):
        captured.append((result, parent_message_id))

    # 拦截辅助方法，验证 daemon 分支内部把 parent_message_id 透传进来（端到端）。
    monkeypatch.setattr(spawner, "_emit_subagent_usage", fake_emit)
    res = await spawner.spawn(
        BUILTIN_GENERAL,
        "do a thing",
        depth=1,
        base_registry=_registry(),
        base_model=_model(),
        parent_handle=parent,
        registry=reg,
        parent_message_id="parent-mid",
    )
    assert res is not None
    assert len(captured) == 1
    result, pmsg = captured[0]
    assert pmsg == "parent-mid"  # 父 message 指针正确
    assert result.message_id != "parent-mid"  # 子 message 拥有独立 message_id


# --------------------------------------------------------------------------- #
# 验收③：USAGE 事件进回放缓冲并经 EVENT 消息转发
# --------------------------------------------------------------------------- #
async def test_bridge_on_event_buffers_usage_for_replay():
    handle = SessionHandle("sid", "n", None, "/tmp/proj")
    conn = _FakeConn()
    handle.attached_conn = conn
    t = BridgeTransport(handle)
    ev = Event(type=EventType.USAGE, message_id="m1", usage={"prompt_tokens": 1}, duration=0.1)
    t._on_event(ev)
    await asyncio.sleep(0)  # 让被 ensure_future 调度的 EVENT 转发任务跑完
    # USAGE 非 transient，进入回放缓冲（供 _replay 重发），并实时转发 EVENT 消息。
    assert ev in handle.event_buffer
    assert any(e.type == EventType.USAGE for e in handle.event_buffer)


async def test_replay_forwards_usage_event():
    handle = SessionHandle("sid", "n", None, "/tmp/proj")
    handle.registry = None  # 无持久化 → 走内存回放路径（等价于前端 reducer 读取入口）
    conn = _FakeConn()
    handle.attached_conn = conn
    ev = Event(type=EventType.USAGE, message_id="m1", usage={"prompt_tokens": 1}, duration=0.1)
    handle.event_buffer.append(ev)
    await _replay(conn, handle, "sid")
    assert any(
        d["type"] == MsgType.EVENT and d["payload"]["event"]["type"] == "usage" for d in conn.sent
    )
