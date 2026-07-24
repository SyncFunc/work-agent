"""M7 守护进程会话注册表（多 Session 索引 + 每会话环形缓冲 + 每会话锁）。

M9.0：注册表升级为**多项目感知**——每个会话绑定 ``project_root``，按项目隔离
settings 与 ``SessionStore``。同一 daemon 进程可服务多个项目，互不串扰。

``SessionRegistry`` 以 ``session_id`` 索引多个 ``SessionHandle``，负责 ``new`` / ``get`` /
``attach`` / ``detach`` / ``switch`` / ``list``。所有会话相关操作都显式携带 ``project_root``
（CLI 缺省回退 cwd，仅用于兼容）。

``SessionHandle`` 持有：
- ``session``：``agent.core.session.Session`` 实例（agentrunner 驱动）。
- ``transport``：``AgentTransport`` 实现（``BridgeTransport`` 等），``session.step`` 的渲染/HITL 承接方。
- ``event_buffer``：每会话定长环形缓冲（``deque(maxlen=K)``），**仅收非 ``transient`` 事件**（修复点②）。
- ``attached_conn``：当前 attach 的前端连接（实时事件转发目标）。
- ``busy``：同步标志，避免并发 ``task.send`` 竞态（配合 ``lock`` 双重保险）。
- ``lock``：每会话 ``asyncio.Lock``，保证同一会话一次仅一个 step 在飞。
- ``project_root``：会话所属项目根（M9.0 多项目隔离维度）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from agent.core.events import Event
from agent.daemon.protocol import MsgType

if TYPE_CHECKING:
    from agent.core.session import SessionLike
    from agent.daemon.bridge import BridgeTransport


DEFAULT_BUFFER_SIZE = 200


@runtime_checkable
class ConnLike(Protocol):
    """daemon 连接的最小接口（server 的 ``Connection`` 与测试用 ``Conn`` 共用）。"""

    session_id: str | None

    async def send(
        self,
        type: MsgType | str,
        payload: dict[str, Any] | None = None,
        *,
        id: str | None = None,
        session: str | None = None,
    ) -> None: ...


class SessionHandle:
    """单个会话的句柄（daemon 内部管理单元）。"""

    def __init__(
        self,
        session_id: str,
        name: str | None,
        session: SessionLike | None,
        transport: BridgeTransport | None,
        project_root: str = "",
    ) -> None:
        self.session_id = session_id
        self.name = name or session_id[:8]
        self.project_root = project_root
        self.session: SessionLike | None = session
        self.transport: BridgeTransport | None = (
            transport  # AgentTransport 实现（BridgeTransport 等）
        )
        # 回放缓冲：仅持久化事件（M7.4 修复点②）。O(1) 追加与截断，防内存膨胀。
        self.event_buffer: deque[Event] = deque(maxlen=DEFAULT_BUFFER_SIZE)
        self.attached_conn: ConnLike | None = None
        self.running = False
        self.busy = False  # 同步标志：避免并发 task.send 竞态
        self.last_activity = time.time()
        self.lock = asyncio.Lock()  # 每会话锁：保证同一会话一个 step 在飞


class SessionRegistry:
    """多会话索引（M9.0 多项目感知）：``new`` / ``get`` / ``attach`` / ``detach`` / ``switch`` / ``list``。

    每个会话绑定 ``project_root``；``SessionStore`` 按项目隔离（``store_factory`` 惰性解析并缓存）。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[str, str], SessionLike] | None = None,
        transport_factory: Callable[[SessionHandle], BridgeTransport] | None = None,
        restore_factory: Callable[[str, str], SessionLike | None] | None = None,
        store_factory: Callable[[str], object] | None = None,
        trace_store_factory: Callable[[str], object] | None = None,
    ) -> None:
        self._sessions: dict[str, SessionHandle] = {}
        # 工厂签名（M9.0）：一律带入 project_root，按项目解析 settings / SessionStore。
        self._session_factory = session_factory  # (project_root, session_id) -> Session
        self._transport_factory = transport_factory
        # M6.2 冷启动恢复：给定 (project_root, session_id)，若该会话已存在于该项目的
        # SessionStore 返回重建的 Session，否则返回 None（走新建）。由 start_daemon 注入。
        self._restore_factory = restore_factory  # (project_root, session_id) -> Session | None
        # M9.0：按 project_root 惰性解析并返回 SessionStore（用于列表/冷启动）。可选。
        self._store_factory = store_factory  # (project_root) -> SessionStore
        # M9.7：按 project_root 惰性解析并返回 TraceStore（供 trace.list/trace.get 查询）。
        self._trace_store_factory = trace_store_factory  # (project_root) -> TraceStore
        self._token: str = ""  # hello 鉴权令牌（可选；由 start_daemon 注入）

    def new(
        self,
        project_root: str,
        name: str | None = None,
        *,
        session_factory: Callable[[str, str], SessionLike] | None = None,
        transport_factory: Callable[[SessionHandle], BridgeTransport] | None = None,
    ) -> SessionHandle:
        sid = uuid.uuid4().hex
        sf = session_factory or self._session_factory
        tf = transport_factory or self._transport_factory
        session = sf(project_root, sid) if sf is not None else None
        handle = SessionHandle(sid, name, session, None, project_root)
        if tf is not None:
            handle.transport = tf(handle)
        self._sessions[sid] = handle
        return handle

    def get(self, session_id: str | None) -> SessionHandle | None:
        if session_id is None:
            return None
        return self._sessions.get(session_id)

    def restore(
        self, session_id: str, session: SessionLike, project_root: str = ""
    ) -> SessionHandle:
        """M6.2 冷启动恢复：为已存在于 SessionStore 的 ``session_id`` 建立句柄（不生成新 id）。

        把会话完整 EventStream 的最近 K 条事件播种进 ``event_buffer``，使后续 attach
        的客户端能回放断点前的上下文（与 M7.4 热切换回放一致）。
        """
        handle = SessionHandle(session_id, session_id[:8], session, None, project_root)
        es = getattr(session, "event_stream", None)
        if es is not None:
            for ev in list(es.all())[-DEFAULT_BUFFER_SIZE:]:
                handle.event_buffer.append(ev)
        self._sessions[session_id] = handle
        return handle

    def attach(self, conn: ConnLike, project_root: str, session_id: str) -> SessionHandle | None:
        """把连接 ``conn`` attach 到项目 ``project_root`` 下的会话；若已被别的连接占用则顶替。

        M6.2 冷启动：若该 id 不在内存但在该项目的 SessionStore 中存在，先经 ``restore_factory``
        恢复会话再 attach（daemon 重启后无需原进程在跑即可 resume）。
        """
        handle = self._sessions.get(session_id)
        if handle is None and self._restore_factory is not None:
            sess = self._restore_factory(project_root, session_id)
            if sess is not None:
                handle = self.restore(session_id, sess, project_root)
        if handle is None:
            return None
        if handle.attached_conn is not None and handle.attached_conn is not conn:
            old = handle.attached_conn
            handle.attached_conn = None
            try:
                asyncio.ensure_future(
                    old.send(MsgType.DETACHED, {"session_id": session_id}, session=session_id)
                )
            except Exception:
                pass
        handle.attached_conn = conn
        conn.session_id = session_id
        return handle

    def detach(self, conn: ConnLike) -> str | None:
        """把连接 ``conn`` 从当前会话 detach，返回被 detach 的 session_id。"""
        sid = getattr(conn, "session_id", None)
        if sid is None:
            return None
        handle = self._sessions.get(sid)
        if handle is not None and handle.attached_conn is conn:
            handle.attached_conn = None
        conn.session_id = None
        return sid

    def switch(self, conn: ConnLike, project_root: str, session_id: str) -> SessionHandle | None:
        """切换 = 先 detach 当前，再 attach 目标。"""
        self.detach(conn)
        return self.attach(conn, project_root, session_id)

    def list_info(self, project_root: str | None = None) -> list[dict]:
        """会话清单（供 ``session_list`` 响应）。

        - 不传 ``project_root``：返回全部内存会话（向后兼容 CLI 旧行为）。
        - 传 ``project_root``：仅返回该项目的内存会话；若 ``store_factory`` 可用，额外合并
          该项目已持久化但不在内存的会话（按 session_id 去重），便于桌面端展示完整列表。
        """
        sessions: list[dict] = []
        seen: set[str] = set()
        for h in self._sessions.values():
            if project_root is not None and h.project_root != project_root:
                continue
            sessions.append(
                {
                    "id": h.session_id,
                    "name": h.name,
                    "project_root": h.project_root,
                    "attached": h.attached_conn is not None,
                    "running": h.running,
                    "last_activity": h.last_activity,
                }
            )
            seen.add(h.session_id)
        # 合并持久化会话（仅限指定项目，避免跨项目串扰）
        if project_root is not None and self._store_factory is not None:
            try:
                for row in self._store_factory(project_root).list_sessions():
                    sid = row["session_id"]
                    if sid in seen:
                        continue
                    sessions.append(
                        {
                            "id": sid,
                            "name": row.get("name") or sid[:8],
                            "project_root": project_root,
                            "attached": False,
                            "running": False,
                            "last_activity": row.get("updated_at"),
                            "persisted": True,
                        }
                    )
                    seen.add(sid)
            except Exception:
                # 存储不可用时退化为仅内存列表（不阻断列表查询）
                pass
        return sessions
