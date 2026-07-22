"""会话持久化存储（M6 会话恢复基础设施）。

把 ``EventStream``（状态单一事实来源）按 ``seq`` 落盘到 sqlite，支持跨重启恢复与
会话 fork（从父会话事件前缀派生新分支）。

设计要点（呼应 M4 双轨铁律）：
- ``EventStream`` 永不压缩；持久化的 events 即完整未压缩序列。
- 仅持久化非 ``transient`` 事件（``tool_call_delta`` 等瞬时事件不落盘，与 M7 回放一致）。
- fork = 复制父 events 前缀到新 ``session_id``（复制语义，非 per-event 链表）。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from agent.core.events import Event, EventStream


class SessionStore:
    """SQLite 持久化：``sessions`` 元数据 + ``events`` 事件流。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id        TEXT PRIMARY KEY,
                    name              TEXT,
                    parent_session_id TEXT,
                    created_at        REAL NOT NULL,
                    updated_at        REAL NOT NULL,
                    plan_mode         INTEGER,
                    plan_path         TEXT,
                    clarify_total     INTEGER,
                    root_span_id      TEXT,
                    model_meta_json   TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    session_id TEXT NOT NULL,
                    seq        INTEGER NOT NULL,
                    type       TEXT NOT NULL,
                    json       TEXT NOT NULL,
                    transient  INTEGER NOT NULL DEFAULT 0,
                    ts         REAL NOT NULL,
                    PRIMARY KEY (session_id, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
                """
            )

    def create(
        self,
        session_id: str,
        name: str | None = None,
        parent_session_id: str | None = None,
        *,
        plan_mode: bool = False,
        plan_path: str | None = None,
        clarify_total: int = 0,
        root_span_id: str | None = None,
        model_meta_json: str | None = None,
    ) -> None:
        """登记一个会话行（幂等：已存在则跳过 INSERT，仅刷新 updated_at）。"""
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO sessions
                   (session_id, name, parent_session_id, created_at, updated_at,
                    plan_mode, plan_path, clarify_total, root_span_id, model_meta_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    name,
                    parent_session_id,
                    now,
                    now,
                    1 if plan_mode else 0,
                    plan_path,
                    clarify_total,
                    root_span_id,
                    model_meta_json,
                ),
            )
            conn.execute(
                "UPDATE sessions SET updated_at=? WHERE session_id=?", (now, session_id)
            )

    def append_event(self, session_id: str, ev: Event) -> None:
        """持久化单条事件（瞬时不落盘）。"""
        if ev.transient:
            return
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO events
                   (session_id, seq, type, json, transient, ts)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    ev.seq,
                    ev.type.value,
                    json.dumps(ev.to_dict(), ensure_ascii=False),
                    1 if ev.transient else 0,
                    ev.ts,
                ),
            )

    def append_events(self, session_id: str, stream: EventStream) -> None:
        for ev in stream.all():
            self.append_event(session_id, ev)

    def load(self, session_id: str) -> EventStream | None:
        """按 session_id 重建完整 EventStream（按 seq 升序）。无记录返回 None。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT json FROM events WHERE session_id=? ORDER BY seq", (session_id,)
            ).fetchall()
        if not rows:
            return None
        items = [json.loads(r["json"]) for r in rows]
        return EventStream.from_json(json.dumps(items, ensure_ascii=False))

    def _next_seq(self, session_id: str) -> int:
        """该会话下一个全局事件序号（MAX(seq)+1，无记录则 0）。

        用于续跑/恢复时让新 run 的事件以「会话级全局 seq」落盘，避免与既有前缀
        的 per-run seq（0,1,2…）碰撞被 ``INSERT OR REPLACE`` 覆盖。
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) FROM events WHERE session_id=?", (session_id,)
            ).fetchone()
        return (row[0] + 1) if row and row[0] is not None else 0

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def get_parent(self, session_id: str) -> str | None:
        s = self.get_session(session_id)
        return s["parent_session_id"] if s else None

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT session_id, name, parent_session_id, created_at, updated_at "
                "FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def touch(self, session_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET updated_at=? WHERE session_id=?",
                (time.time(), session_id),
            )

    def fork(self, parent_session_id: str, name: str | None = None) -> str:
        """从父会话派生新分支：复制父 events 前缀到新 session_id。

        返回新 ``session_id``；``parent_session_id`` 记录血缘（用于 list 展示）。
        """
        new_id = uuid.uuid4().hex
        self.create(new_id, name=name, parent_session_id=parent_session_id)
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO events (session_id, seq, type, json, transient, ts)
                   SELECT ?, seq, type, json, transient, ts
                   FROM events WHERE session_id=? ORDER BY seq""",
                (new_id, parent_session_id),
            )
        return new_id


class SessionStoreSink:
    """``EventStream`` 订阅器：每次 append 非 transient 事件即落盘（零侵入持久化）。

    用法：``stream.subscribe(SessionStoreSink(store, session_id))``。``loop.run`` 在
    创建 ``EventStream`` 后订阅它，无需改动循环主逻辑即可持久化。

    关键：落盘时用「会话级全局 seq」覆盖事件自带的 per-run seq（每个新 run 的
    ``EventStream`` 从 0 重新开始），否则续跑/恢复时新事件 seq 会与既有前缀碰撞
    被 ``INSERT OR REPLACE`` 覆盖，破坏事件流完整性。
    """

    def __init__(self, store: SessionStore, session_id: str) -> None:
        self._store = store
        self._session_id = session_id
        self._seq: int | None = None  # 惰性初始化为该会话 DB 最大 seq + 1

    def __call__(self, ev: Event) -> None:
        if ev.transient:
            return
        if self._seq is None:
            self._seq = self._store._next_seq(self._session_id)
        # 用会话级全局 seq 覆盖事件自带 seq（同一 Event 对象，in-memory 流同步更新）
        ev.seq = self._seq
        self._seq += 1
        self._store.append_event(self._session_id, ev)
