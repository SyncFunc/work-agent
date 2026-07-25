"""SQLite 持久化存储：按 session 归档 trace/span/log。

表结构：
- ``spans``：session_id / span_id / name / kind / parent_id / 起止时间 / meta_json
- ``logs``：session_id / span_id / ts / key / value / level

``save_trace`` 覆盖写（先删后插），保证幂等。
``load_trace`` 重建 Span 对象（含 logs 列表）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from agent.obs.tracer import LogEntry, Span, Tracer


class TraceStore:
    """SQLite 持久化 trace。"""

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
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS spans (
                    session_id TEXT NOT NULL,
                    span_id    TEXT NOT NULL,
                    name       TEXT NOT NULL,
                    kind       TEXT NOT NULL DEFAULT 'span',
                    parent_id  TEXT,
                    started_at REAL NOT NULL,
                    ended_at   REAL,
                    meta_json  TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL DEFAULT (julianday('now')),
                    PRIMARY KEY (session_id, span_id)
                );
                CREATE TABLE IF NOT EXISTS logs (
                    session_id TEXT NOT NULL,
                    span_id    TEXT NOT NULL,
                    ts         REAL NOT NULL,
                    key        TEXT NOT NULL,
                    value      TEXT NOT NULL DEFAULT '',
                    level      TEXT NOT NULL DEFAULT 'info',
                    PRIMARY KEY (session_id, span_id, ts, key)
                );
                CREATE INDEX IF NOT EXISTS idx_spans_session ON spans(session_id);
                CREATE INDEX IF NOT EXISTS idx_logs_session ON logs(session_id);
            """)

    def save_trace(self, tracer: Tracer) -> None:
        """持久化一个 Tracer 的全部 span（含 logs）。覆盖写保证幂等。"""
        session_id = tracer.session_id
        with self._conn() as conn:
            conn.execute("DELETE FROM logs WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM spans WHERE session_id = ?", (session_id,))

            for s in tracer.spans:
                conn.execute(
                    """INSERT INTO spans
                       (session_id, span_id, name, kind, parent_id, started_at, ended_at, meta_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        s.id,
                        s.name,
                        s.kind,
                        s.parent_id,
                        s.started_at,
                        s.ended_at,
                        json.dumps(s.meta, ensure_ascii=False, default=str),
                    ),
                )
                for lg in s.logs:
                    conn.execute(
                        """INSERT INTO logs
                           (session_id, span_id, ts, key, value, level)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (session_id, s.id, lg.ts, lg.key, _serialize_value(lg.value), lg.level),
                    )

    def load_trace(self, session_id: str) -> Tracer | None:
        """按 session_id 重建 Tracer（含全部 span + logs）。不存在返回 None。"""
        with self._conn() as conn:
            span_rows = conn.execute(
                "SELECT * FROM spans WHERE session_id = ? ORDER BY started_at",
                (session_id,),
            ).fetchall()
            if not span_rows:
                return None
            log_rows = conn.execute(
                "SELECT * FROM logs WHERE session_id = ? ORDER BY ts",
                (session_id,),
            ).fetchall()
        logs_by_span: dict[str, list[LogEntry]] = {}
        for lr in log_rows:
            logs_by_span.setdefault(lr["span_id"], []).append(
                LogEntry(ts=lr["ts"], key=lr["key"], value=lr["value"], level=lr["level"])
            )
        tracer = Tracer(session_id=session_id)
        for sr in span_rows:
            meta: dict[str, Any] = {}
            try:
                meta = json.loads(sr["meta_json"]) if sr["meta_json"] else {}
            except (json.JSONDecodeError, TypeError):
                pass
            s = Span(
                id=sr["span_id"],
                name=sr["name"],
                kind=sr["kind"],
                parent_id=sr["parent_id"],
                started_at=sr["started_at"],
                ended_at=sr["ended_at"],
                meta=meta,
                logs=logs_by_span.get(sr["span_id"], []),
            )
            tracer.spans.append(s)
        return tracer

    def list_sessions(self) -> list[dict[str, Any]]:
        """返回所有有记录的 session 信息列表（按创建时间降序）。"""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT session_id, COUNT(*) as span_count,
                          MIN(started_at) as first_ts, MAX(started_at) as last_ts
                   FROM spans GROUP BY session_id ORDER BY last_ts DESC"""
            ).fetchall()
            return [
                {
                    "session_id": r["session_id"],
                    "span_count": r["span_count"],
                    "first_ts": r["first_ts"],
                    "last_ts": r["last_ts"],
                }
                for r in rows
            ]

    def list_traces(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """返回含 trace（span）的会话列表（供 M9.7 daemon ``trace.list``）。

        - ``session_id`` 为 None：返回全部项目的 trace（按 last_ts 降序）；
        - 指定 ``session_id``：仅返回该会话的 trace 摘要（命中 0/1 条）。
        """
        with self._conn() as conn:
            if session_id:
                rows = conn.execute(
                    """SELECT session_id, COUNT(*) as span_count,
                              MIN(started_at) as first_ts, MAX(started_at) as last_ts
                       FROM spans WHERE session_id = ? GROUP BY session_id""",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT session_id, COUNT(*) as span_count,
                              MIN(started_at) as first_ts, MAX(started_at) as last_ts
                       FROM spans GROUP BY session_id ORDER BY last_ts DESC"""
                ).fetchall()
            return [
                {
                    "session_id": r["session_id"],
                    "span_count": r["span_count"],
                    "first_ts": r["first_ts"],
                    "last_ts": r["last_ts"],
                }
                for r in rows
            ]


def _serialize_value(v: Any) -> str:
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(v)
