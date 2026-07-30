"""SQLite 持久化存储：归档 trace/span/log（按 session 全量覆盖写，逐步追加 trace_id）。

M5.8 关键变更：
- 新增 ``trace_id`` 列（存储于 span.meta["trace_id"]），用于按一次用户操作检索
- ``save_trace`` 仍按 session 全量删除重插（保障幂等），trace_id 从每个 span 的 meta 提取
- ``load_trace`` 支持按 trace_id（per-op）或按 session_id（完整会话）加载，自动回退
- ``list_traces`` 按 trace_id 分组返回多条（一个 session 可能有多次用户操作）
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
        # M5.8 迁移：为已有数据库补 trace_id 列 + 索引。
        # ！！必须与 CREATE TABLE 分离，否则对旧 DB 文件（无 trace_id 列）
        #   执行 CREATE INDEX ON trace_id 会抛 "no such column" 异常，
        #   整个 executescript 中断，迁移逻辑永远无法运行。
        self._migrate_add_trace_id()

    def _migrate_add_trace_id(self) -> None:
        with self._conn() as conn:
            for table in ("spans", "logs"):
                try:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN trace_id TEXT NOT NULL DEFAULT ''"
                    )
                except sqlite3.OperationalError:
                    pass  # 列已存在
            for table, idx_name in (
                ("spans", "idx_spans_trace"),
                ("logs", "idx_logs_trace"),
            ):
                try:
                    conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table}(trace_id)")
                except sqlite3.OperationalError:
                    pass

    def save_trace(self, tracer: Tracer) -> None:
        """持久化一个 Tracer 的全部 span（含 logs）。覆盖写保证幂等。"""
        session_id = tracer.session_id
        with self._conn() as conn:
            conn.execute("DELETE FROM logs WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM spans WHERE session_id = ?", (session_id,))

            for s in tracer.spans:
                tid = s.meta.get("trace_id", "")
                conn.execute(
                    """INSERT INTO spans
                       (session_id, trace_id, span_id, name, kind, parent_id,
                        started_at, ended_at, meta_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        tid,
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
                           (session_id, trace_id, span_id, ts, key, value, level)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            session_id,
                            tid,
                            s.id,
                            lg.ts,
                            lg.key,
                            _serialize_value(lg.value),
                            lg.level,
                        ),
                    )

    def delete_session(self, session_id: str) -> None:
        """彻底删除该会话的全部 span 与 log。"""
        with self._conn() as conn:
            conn.execute("DELETE FROM logs WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM spans WHERE session_id = ?", (session_id,))

    def load_trace(self, trace_or_session_id: str) -> Tracer | None:
        """按 trace_id 或 session_id 加载 Tracer。

        - 先按 trace_id 精确匹配（trace_id = message_id，一次用户操作）；
        - 如果没找到，退化为按 session_id 加载所有 span（向后兼容）。
        不存在返回 None。
        """
        with self._conn() as conn:
            # Primary: 按 trace_id 精确查询
            span_rows = conn.execute(
                "SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at",
                (trace_or_session_id,),
            ).fetchall()
            if not span_rows:
                # Fallback: 按 session_id 查询（兼容旧数据/测试）
                span_rows = conn.execute(
                    "SELECT * FROM spans WHERE session_id = ? ORDER BY started_at",
                    (trace_or_session_id,),
                ).fetchall()
            if not span_rows:
                return None

            log_rows = conn.execute(
                "SELECT * FROM logs WHERE session_id = ? ORDER BY ts",
                (span_rows[0]["session_id"],),
            ).fetchall()
        return self._build_tracer(span_rows, log_rows)

    def load_session_traces(self, session_id: str) -> list[Tracer]:
        """按 session_id 加载该会话下的所有 trace（多条，逐次用户操作）。"""
        with self._conn() as conn:
            trace_ids = conn.execute(
                "SELECT DISTINCT trace_id FROM spans WHERE session_id = ? AND trace_id != ''",
                (session_id,),
            ).fetchall()
            tracers: list[Tracer] = []
            for (tid,) in trace_ids:
                span_rows = conn.execute(
                    "SELECT * FROM spans WHERE session_id = ? AND trace_id = ? ORDER BY started_at",
                    (session_id, tid),
                ).fetchall()
                log_rows = conn.execute(
                    "SELECT * FROM logs WHERE session_id = ? AND trace_id = ? ORDER BY ts",
                    (session_id, tid),
                ).fetchall()
                t = self._build_tracer(span_rows, log_rows)
                if t is not None:
                    tracers.append(t)
            return tracers

    def load_all_session_spans(self, session_id: str) -> Tracer | None:
        """按 session_id 加载全部 span（完整会话视图）。不存在返回 None。"""
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
        return self._build_tracer(span_rows, log_rows)

    def _build_tracer(
        self, span_rows: list[sqlite3.Row], log_rows: list[sqlite3.Row]
    ) -> Tracer | None:
        if not span_rows:
            return None
        logs_by_span: dict[str, list[LogEntry]] = {}
        for lr in log_rows:
            logs_by_span.setdefault(lr["span_id"], []).append(
                LogEntry(ts=lr["ts"], key=lr["key"], value=lr["value"], level=lr["level"])
            )
        tracer = Tracer(session_id=span_rows[0]["session_id"])
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
        """返回 trace 列表（按 trace_id 分组）。

        有 trace_id 的按 trace_id 分组（一次用户操作一条）；trace_id 为空的
        按 session_id 分组（兼容旧数据/测试），此时 trace_id = session_id。

        - session_id 为 None：返回全部 trace（按 last_ts 降序）；
        - 指定 session_id：仅返回该会话下的 trace 列表。
        """
        with self._conn() as conn:
            if session_id:
                rows = conn.execute(
                    """SELECT
                        CASE WHEN trace_id = '' THEN session_id ELSE trace_id END as trace_id,
                        session_id,
                        COUNT(*) as span_count,
                        MIN(started_at) as first_ts,
                        MAX(started_at) as last_ts
                       FROM spans
                       WHERE session_id = ?
                       GROUP BY CASE WHEN trace_id = '' THEN session_id ELSE trace_id END
                       ORDER BY last_ts DESC""",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT
                        CASE WHEN trace_id = '' THEN session_id ELSE trace_id END as trace_id,
                        session_id,
                        COUNT(*) as span_count,
                        MIN(started_at) as first_ts,
                        MAX(started_at) as last_ts
                       FROM spans
                       GROUP BY CASE WHEN trace_id = '' THEN session_id ELSE trace_id END
                       ORDER BY last_ts DESC"""
                ).fetchall()
            return [
                {
                    "trace_id": r["trace_id"],
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
