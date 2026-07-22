"""M7 守护进程会话注册表（多 Session 索引 + 每会话环形缓冲 + 每会话锁）。

``SessionRegistry`` 以 ``session_id`` 索引多个 ``SessionHandle``，负责 ``new`` / ``get`` /
``attach`` / ``detach`` / ``switch`` / ``list``。

``SessionHandle`` 持有：
- ``session``：``agent.core.session.Session`` 实例（agentrunner 驱动）。
- ``transport``：``AgentTransport`` 实现（``BridgeTransport`` 等），``session.step`` 的渲染/HITL 承接方。
- ``event_buffer``：每会话定长环形缓冲（``deque(maxlen=K)``），**仅收非 ``transient`` 事件**（修复点②）。
- ``attached_conn``：当前 attach 的前端连接（实时事件转发目标）。
- ``busy``：同步标志，避免并发 ``task.send`` 竞态（配合 ``lock`` 双重保险）。
- ``lock``：每会话 ``asyncio.Lock``，保证同一会话一次仅一个 step 在飞。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

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
        type: "MsgType | str",
        payload: dict[str, Any] | None = None,
        *,
        id: str | None = None,
        session: str | None = None,
    ) -> None: ...


class SessionHandle:
    """单个会话的句柄（daemon 内部管理单元）。"""

    def __init__(
        self, session_id: str, name: str, session: "SessionLike | None", transport: "BridgeTransport | None"
    ) -> None:
        self.session_id = session_id
        self.name = name or session_id[:8]
        self.session: SessionLike | None = session
        self.transport: BridgeTransport | None = transport  # AgentTransport 实现（BridgeTransport 等）
        # 回放缓冲：仅持久化事件（M7.4 修复点②）。O(1) 追加与截断，防内存膨胀。
        self.event_buffer: deque[Event] = deque(maxlen=DEFAULT_BUFFER_SIZE)
        self.attached_conn: ConnLike | None = None
        self.running = False
        self.busy = False  # 同步标志：避免并发 task.send 竞态
        self.last_activity = time.time()
        self.lock = asyncio.Lock()  # 每会话锁：保证同一会话一个 step 在飞


class SessionRegistry:
    """多会话索引：``new`` / ``get`` / ``attach`` / ``detach`` / ``switch`` / ``list``。"""

    def __init__(
        self,
        *,
        session_factory: "Callable[[], SessionLike] | None" = None,
        transport_factory: "Callable[[SessionHandle], BridgeTransport] | None" = None,
    ) -> None:
        self._sessions: dict[str, SessionHandle] = {}
        self._session_factory = session_factory
        self._transport_factory = transport_factory
        self._token: str = ""  # hello 鉴权令牌（可选；由 start_daemon 注入）

    def new(
        self,
        name: str | None = None,
        *,
        session_factory: "Callable[[], SessionLike] | None" = None,
        transport_factory: "Callable[[SessionHandle], BridgeTransport] | None" = None,
    ) -> SessionHandle:
        sid = uuid.uuid4().hex
        sf = session_factory or self._session_factory
        tf = transport_factory or self._transport_factory
        session = sf() if sf is not None else None
        handle = SessionHandle(sid, name, session, None)
        if tf is not None:
            handle.transport = tf(handle)
        self._sessions[sid] = handle
        return handle

    def get(self, session_id: str | None) -> SessionHandle | None:
        if session_id is None:
            return None
        return self._sessions.get(session_id)

    def attach(self, conn: "ConnLike", session_id: str) -> SessionHandle | None:
        """把连接 ``conn`` attach 到会话；若已被别的连接占用则顶替（通知旧连接）。"""
        handle = self._sessions.get(session_id)
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

    def detach(self, conn: "ConnLike") -> str | None:
        """把连接 ``conn`` 从当前会话 detach，返回被 detach 的 session_id。"""
        sid = getattr(conn, "session_id", None)
        if sid is None:
            return None
        handle = self._sessions.get(sid)
        if handle is not None and handle.attached_conn is conn:
            handle.attached_conn = None
        conn.session_id = None
        return sid

    def switch(self, conn: "ConnLike", session_id: str) -> SessionHandle | None:
        """切换 = 先 detach 当前，再 attach 目标。"""
        self.detach(conn)
        return self.attach(conn, session_id)

    def list_info(self) -> list[dict]:
        """会话清单（供 ``session_list`` 响应）。"""
        return [
            {
                "id": h.session_id,
                "name": h.name,
                "attached": h.attached_conn is not None,
                "running": h.running,
                "last_activity": h.last_activity,
            }
            for h in self._sessions.values()
        ]
