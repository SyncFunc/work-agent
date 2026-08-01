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
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from agent.core.events import Event, EventStream

# 会话标题来源（M11.6）：优先级 manual > memory > user。
# - user：会话首个用户提问（自动生成，可被 memory/manual 覆盖）；
# - memory：后台 session-memory 子 agent 生成的 Session Title（可被 manual 覆盖）；
# - manual：用户手动编辑（锁定，永不被自动覆盖）。
TITLE_SOURCE_USER = "user"
TITLE_SOURCE_MEMORY = "memory"
TITLE_SOURCE_MANUAL = "manual"


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
            # 注意：索引 idx_events_parent 依赖 parent_session_id 列，必须先确保该列存在
            # （旧库迁移）再建索引，否则在「已存在旧 events 表」上 CREATE INDEX 会先于 ALTER
            # 执行而报 no such column。故该索引放在 ALTER 之后单独建。
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
                    model_meta_json   TEXT,
                    title             TEXT,
                    title_source      TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    session_id TEXT NOT NULL,
                    seq        INTEGER NOT NULL,
                    type       TEXT NOT NULL,
                    json       TEXT NOT NULL,
                    transient  INTEGER NOT NULL DEFAULT 0,
                    ts         REAL NOT NULL,
                    parent_session_id TEXT,
                    PRIMARY KEY (session_id, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
                """
            )
            # M9 subsession：旧库无 parent_session_id 列时做在线迁移（不破坏既有数据）。
            try:
                conn.execute("ALTER TABLE events ADD COLUMN parent_session_id TEXT")
            except sqlite3.OperationalError:
                pass  # 列已存在
            # M11.6 会话标题：旧库无 title / title_source 列时在线迁移（不破坏既有数据）。
            for col, ddl in (
                ("title", "TEXT"),
                ("title_source", "TEXT"),
            ):
                try:
                    conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError:
                    pass  # 列已存在
            # 迁移/新建后该列必然存在，再建父会话索引。
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_parent ON events(parent_session_id)"
                )
            except sqlite3.OperationalError:
                pass

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
        title: str | None = None,
        title_source: str | None = None,
    ) -> None:
        """登记一个会话行（幂等：已存在则跳过 INSERT，仅刷新 updated_at）。"""
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO sessions
                   (session_id, name, parent_session_id, created_at, updated_at,
                    plan_mode, plan_path, clarify_total, root_span_id, model_meta_json,
                    title, title_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    title,
                    title_source,
                ),
            )
            conn.execute("UPDATE sessions SET updated_at=? WHERE session_id=?", (now, session_id))

    def append_event(
        self, session_id: str, ev: Event, parent_session_id: str | None = None
    ) -> None:
        """持久化单条事件（瞬时不落盘）。

        M9 subsession：``parent_session_id`` 非空时标记为「子会话事件」，供按父会话查询
        （``iter_events_with_subsession``）重建主聊天里的独立子 agent 块历史。
        """
        if ev.transient:
            return
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO events
                   (session_id, seq, type, json, transient, ts, parent_session_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    ev.seq,
                    ev.type.value,
                    json.dumps(ev.to_dict(), ensure_ascii=False),
                    1 if ev.transient else 0,
                    ev.ts,
                    parent_session_id,
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

    def iter_events_with_subsession(self, session_id: str) -> list[tuple[Event, str | None]]:
        """按 ``session_id`` 取父会话事件 + 其全部子会话事件，按时间序返回 ``(Event, subsession_id)``。

        - 父会话事件：``subsession_id = None``；
        - 子会话事件：``subsession_id`` = 子会话自身的 ``session_id``（即前端分组用的 id）。

        供 daemon 回放重建主聊天历史（含独立子 agent 块），并修复「重进后历史变少」：
        不再受内存 ``event_buffer``（maxlen）限制，改为读取 sqlite 全量。
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT json, parent_session_id AS sub_id, ts, seq FROM events WHERE session_id=?
                UNION ALL
                SELECT json, session_id AS sub_id, ts, seq FROM events WHERE parent_session_id=?
                ORDER BY ts, seq
                """,
                (session_id, session_id),
            ).fetchall()
        return [(Event.from_dict(json.loads(r["json"])), r["sub_id"]) for r in rows]

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT session_id, name, parent_session_id, created_at, updated_at, "
                "title, title_source FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def set_title(self, session_id: str, title: str, source: str = TITLE_SOURCE_MANUAL) -> None:
        """设置会话标题并刷新 ``updated_at``（持久化，重进保留）。

        标题按 ``source`` 记录来源，供上层做优先级决策；本方法只落盘，不判断优先级
        （优先级由调用方在取用标题前决定，见 ``resolve_title``）。
        """
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET title=?, title_source=?, updated_at=? WHERE session_id=?",
                (title, source, time.time(), session_id),
            )

    def resolve_title(self, session_id: str) -> str | None:
        """解析该会话的显示标题（按优先级 manual > memory > user）。

        仅读取已持久化的 title 字段；若为 None 返回 None（由调用方回退到 id 前缀）。
        重进程序后调用即可得到符合预期的标题。
        """
        s = self.get_session(session_id)
        if not s or not s.get("title"):
            return None
        return s["title"]

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

    def delete_session(
        self,
        session_id: str,
        *,
        trace_store: Any | None = None,
        session_memory_dir: str | None = None,
    ) -> None:
        """M9.9 彻底删除会话。

        删除范围（彻底删除）：
        - sqlite 行：会话元数据（含全部子会话）+ 事件流（含全部子会话事件）；
        - 关联的 trace（经 ``trace_store.delete_session``，含子会话）；
        - 关联的 Session Memory 目录 ``<session_memory_dir>/<session_id>``（含子会话）。

        子会话集合先经 ``parent_session_id`` 查询收集，再统一清理，保证级联彻底。
        """
        # 收集自身 + 全部子孙会话 id
        ids: list[str] = [session_id]
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT session_id FROM sessions WHERE parent_session_id=?", (session_id,)
            ).fetchall()
            ids.extend(r["session_id"] for r in rows)
            placeholders = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM events WHERE session_id IN ({placeholders})", ids)
            conn.execute(f"DELETE FROM sessions WHERE session_id IN ({placeholders})", ids)
        # trace（duck typing：可选，失败不阻断）
        if trace_store is not None and hasattr(trace_store, "delete_session"):
            try:
                trace_store.delete_session(session_id)
            except Exception:
                pass
        # Session Memory 目录（含子会话）
        if session_memory_dir:
            base = Path(session_memory_dir)
            for sid in ids:
                d = base / sid
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)


class SessionStoreSink:
    """``EventStream`` 订阅器：每次 append 非 transient 事件即落盘（零侵入持久化）。

    用法：``stream.subscribe(SessionStoreSink(store, session_id))``。``loop.run`` 在
    创建 ``EventStream`` 后订阅它，无需改动循环主逻辑即可持久化。

    关键：落盘时用「会话级全局 seq」覆盖事件自带的 per-run seq（每个新 run 的
    ``EventStream`` 从 0 重新开始），否则续跑/恢复时新事件 seq 会与既有前缀碰撞
    被 ``INSERT OR REPLACE`` 覆盖，破坏事件流完整性。

    M9 subsession：``parent_session_id`` 非空时把事件标为子会话事件（带父 id 落盘）。
    """

    def __init__(
        self, store: SessionStore, session_id: str, parent_session_id: str | None = None
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._parent_session_id = parent_session_id

    def __call__(self, ev: Event) -> None:
        if ev.transient:
            return
        # EventStream 现由调用方保证「会话级全局 seq」（跨轮复用同一 stream，
        # append 即 len 递增），故直接落盘 ev.seq，无需改写（旧补丁已移除）。
        self._store.append_event(self._session_id, ev, self._parent_session_id)
