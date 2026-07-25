"""Step A + Step B 验收：EventStream 提升为 session 级唯一真相，且运行期内存封顶。

验证：
- loop.run 复用外部 stream 时 seq 会话级全局单调递增（不再每轮从 0 起）。
- session 跨多轮 step 累积同一 event_stream，seq 全局唯一。
- resume 后重建的 event_stream 新事件 seq 接续（不多重 0 起）。
- SessionStoreSink 不再改写 seq（落盘 seq == 内存 seq）。
- daemon _replay 不再重编 seq，直接发送事件自带全局 seq。
- Step B：EventStream(maxlen) 截断列表后 seq 仍单调全局唯一（不回退/复用）。
- Step B：stream.tail(maxlen) 保留最近 K 条真实 seq，续写从最大 seq+1 起。
"""

from __future__ import annotations

import asyncio
import uuid

from agent.config.settings import Settings
from agent.context.session_store import SessionStore, SessionStoreSink
from agent.core.events import Event, EventStream, EventType
from agent.core.loop import AgentLoop
from agent.core.model import Decision, FakeModel
from agent.core.session import Session
from agent.daemon.protocol import MsgType
from agent.daemon.registry import SessionHandle
from agent.daemon.server import _replay
from agent.runtime.registry import default_registry
from agent.runtime.terminal_transport import TerminalTransport


def _settings() -> Settings:
    s = Settings()
    s.context.session_memory_enabled = False
    s.context.auto_compact_enabled = False
    s.context.microcompact_enabled = False
    s.obs.enabled = False
    return s


def test_loop_reuse_stream_global_seq():
    """loop.run 复用同一 stream 跨多次调用，seq 全局单调递增（不每轮 0 起）。"""
    loop = AgentLoop(FakeModel([Decision(text="x")]), default_registry, _settings())
    stream = EventStream()
    r1 = asyncio.run(loop.run("t1", messages=[], stream=stream))
    n1 = len(r1.events.all())
    assert n1 > 0
    r2 = asyncio.run(loop.run("t2", messages=[], stream=stream))
    evs2 = r2.events.all()
    # r2.events 是整个累积 stream（含第一轮）；第二轮新追加的事件从索引 n1 开始，
    # 其 seq 接续第一轮末尾，而非重置为 0。
    assert len(evs2) > n1
    assert evs2[n1].seq == n1
    assert evs2[-1].seq == len(evs2) - 1


def test_session_event_stream_accumulates_across_steps(tmp_path):
    """同一 session 多次 step，event_stream 跨轮累积且 seq 全局唯一。"""
    store = SessionStore(tmp_path / "s.db")
    sid = uuid.uuid4().hex
    store.create(sid)
    s = Session(
        FakeModel([Decision(text="first answer")]),
        default_registry,
        _settings(),
        session_id=sid,
        session_store=store,
    )
    asyncio.run(s.step("first", TerminalTransport(interactive=False)))
    seqs1 = [e.seq for e in s.event_stream.all()]
    assert seqs1 == sorted(seqs1)
    asyncio.run(s.step("second", TerminalTransport(interactive=False)))
    seqs2 = [e.seq for e in s.event_stream.all()]
    # 全局单调、且比第一轮更长（接续增长）
    assert seqs2 == sorted(seqs2)
    assert max(seqs2) > max(seqs1)
    assert len(seqs2) > len(seqs1)
    # 落盘与内存一致
    assert [e.seq for e in store.load(sid).all()] == seqs2


def test_resume_seq_continues(tmp_path):
    """resume 后新事件 seq 接续重建前缀，全局唯一且不重复 0 起。"""
    store = SessionStore(tmp_path / "s.db")
    sid = uuid.uuid4().hex
    store.create(sid)
    s = Session(
        FakeModel([Decision(text="first answer")]),
        default_registry,
        _settings(),
        session_id=sid,
        session_store=store,
    )
    asyncio.run(s.step("first", TerminalTransport(interactive=False)))
    last_before = max(e.seq for e in s.event_stream.all())

    restored = Session.from_store(
        FakeModel([Decision(text="second answer")]), default_registry, _settings(), store, sid
    )
    assert max(e.seq for e in restored.event_stream.all()) == last_before
    asyncio.run(restored.step("second", TerminalTransport(interactive=False)))
    seqs = [e.seq for e in restored.event_stream.all()]
    assert seqs == sorted(seqs)
    assert max(seqs) > last_before
    # 落盘 seq 与内存一致（证明 Sink 未改写）
    assert [e.seq for e in store.load(sid).all()] == seqs


def test_session_store_sink_keeps_seq(tmp_path):
    """SessionStoreSink 不再改写 seq：落盘 seq 等于内存 ev.seq。"""
    store = SessionStore(tmp_path / "s.db")
    store.create("s1")
    sink = SessionStoreSink(store, "s1")
    es = EventStream()
    es.subscribe(sink)
    es.append(Event(type=EventType.USER, text="hi"))
    es.append(Event(type=EventType.FINAL, text="x"))
    assert [e.seq for e in es.all()] == [0, 1]
    # 落盘 seq 保持 0/1（未被改写为其它值）
    assert [e.seq for e in store.load("s1").all()] == [0, 1]


class _FakeConn:
    def __init__(self) -> None:
        self.sent: list[tuple[object, dict]] = []

    async def send(self, type: object, payload: dict, *, id=None, session=None) -> None:
        self.sent.append((type, payload))


def test_replay_preserves_global_seq():
    """_replay 直接发送事件自带全局 seq，不再重编为 0..n（用不连续 seq 证伪重编）。"""
    handle = SessionHandle("s1", None, None, None)
    # 模拟跨轮累积缓冲：seq 为全局且不连续
    handle.event_buffer.append(Event(type=EventType.USER, text="a", seq=0))  # seq 0
    handle.event_buffer.append(Event(type=EventType.DECISION, decision=Decision(text="x"), seq=1))
    handle.event_buffer.append(Event(type=EventType.TEXT, text="y", seq=5))
    handle.event_buffer.append(Event(type=EventType.FINAL, text="z", seq=6))
    conn = _FakeConn()
    asyncio.run(_replay(conn, handle, "s1"))
    ev_seqs = [p["event"]["seq"] for (t, p) in conn.sent if t == MsgType.EVENT]
    # 保持全局 seq（0,1,5,6），若被旧逻辑重编则为 0,1,2,3
    assert ev_seqs == [0, 1, 5, 6]


def test_stream_maxlen_trims_but_keeps_global_seq():
    """Step B：maxlen 截断后 seq 仍单调全局唯一，不因列表缩短而复用 seq。"""
    stream = EventStream(maxlen=3)
    for i in range(10):
        stream.append(Event(type=EventType.TEXT, text=f"t{i}"))
    evs = stream.all()
    # 仅保留最近 3 条
    assert len(evs) == 3
    # seq 仍为全局唯一、严格递增（7,8,9），而非 0,1,2（旧 len 语义会回退）
    assert [e.seq for e in evs] == [7, 8, 9]
    # 继续追加，seq 续接、不重复
    stream.append(Event(type=EventType.TEXT, text="t10"))
    assert stream.all()[-1].seq == 10
    assert len({e.seq for e in stream.all()}) == len(stream.all())


def test_stream_tail_preserves_true_seq_and_continues():
    """Step B：tail(maxlen) 保留最近 K 条真实 seq，续写从最大 seq+1 起。"""
    full = EventStream()
    for i in range(7):
        full.append(Event(type=EventType.TEXT, text=f"t{i}"))  # seq 0..6
    capped = full.tail(3)
    assert len(capped.all()) == 3
    assert [e.seq for e in capped.all()] == [4, 5, 6]  # 真实历史 seq 保留
    capped.append(Event(type=EventType.TEXT, text="new"))
    assert capped.all()[-1].seq == 7  # 续接最大 seq+1
    # maxlen=3 触发截断：seq 4 被挤出，窗口变为 [5,6,7]；seq 仍全局唯一、无重复。
    assert [e.seq for e in capped.all()] == [5, 6, 7]
    assert len({e.seq for e in capped.all()}) == len(capped.all())
