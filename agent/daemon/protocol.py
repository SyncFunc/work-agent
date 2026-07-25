"""M7 WebSocket 协议：消息信封与编解码（client / server 共用）。

信封（JSON）：
    {"type": str, "id"?: str, "session"?: str, "payload": { ... }}

- ``type``：``MsgType`` 枚举值（见下）。
- ``id``：仅 HITL 请求 / 应答配对（daemon 生成 ``uuid4().hex``，客户端原样回传）。
- ``session``：多数消息带，标识目标会话。
- ``payload``：消息体（各类型自定）。

方向约定（同字符串可双向，方向由发送方隐含）：
- Client → Server：``hello`` / ``session.new`` / ``session.attach`` / ``session.switch`` /
  ``session.detach`` / ``session.list`` / ``task.send`` / ``answer`` / ``confirm_plan`` /
  ``approve`` / ``command`` / ``trace.list`` / ``trace.get``
- Server → Client：``welcome`` / ``session.created`` / ``attached`` / ``detached`` /
  ``session_list`` / ``event`` / ``replay_start`` / ``replay_end`` / ``ask`` /
  ``show_questions`` / ``show_plan`` / ``show_skills`` / ``show_agents`` / ``notify`` /
  ``usage`` / ``close`` / ``error`` / ``trace_list`` / ``trace_tree``

M9.7 可观测面板：新增 trace 查询
- ``trace.list``（C→S）：``{project_root, session_id?}`` → 按项目根列出含 trace 的会话（带 ``id`` 以便响应配对）。
- ``trace.get``（C→S）：``{project_root, trace_id}``（trace_id == session_id）→ 取单条 trace 的 span 树。
- ``trace_list``（S→C）：``{project_root, traces[], id?}``，每项为 ``{session_id, span_count, first_ts, last_ts}``。
- ``trace_tree``（S→C）：``{session_id, spans[], id?}``，span 含 ``parent_id`` 供客户端重建父子树。

事件 ``event`` 直接承载 ``Event.to_dict()``，是天然序列化边界（见 M7.2 知识沉淀）。

M9.0 多项目感知：会话相关消息的 payload 携带 ``project_root``（项目根绝对路径），用于 daemon
按项目隔离 settings 与 ``SessionStore``：

- ``session.new``：``{name?, project_root}``（必带；CLI 缺省回退 cwd）
- ``session.attach``：``{session_id, project_root}``
- ``session.switch``：``{session_id, project_root}``
- ``session.list``：``{project_root}`` → 仅列该项目会话；响应 ``{project_root, sessions}``
- ``session.created`` / ``attached``：响应附带 ``project_root``
- ``task.send``：会话已绑定 project_root，通常无需重复携带；保留 ``{text, yes?, plan?}``

缺省规则：客户端未带 ``project_root`` 时，daemon 回退为 ``os.getcwd()``（仅用于 CLI 向后兼容）；
任何 UI 调用都应显式携带目标项目根。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

DAEMON_VERSION = "0.1.0"
PROTOCOL_VERSION = "1.0"


class MsgType(StrEnum):
    """协议消息类型（值即线上的 ``type`` 字符串）。"""

    # ---- Client -> Server ----
    HELLO = "hello"
    SESSION_NEW = "session.new"
    SESSION_ATTACH = "session.attach"
    SESSION_SWITCH = "session.switch"
    SESSION_DETACH = "session.detach"
    SESSION_LIST = "session.list"
    TASK_SEND = "task.send"
    ANSWER = "answer"
    CONFIRM_PLAN = "confirm_plan"  # 客户端回传：{id, confirmed}
    APPROVE = "approve"  # 客户端回传：{id, approved}
    COMMAND = "command"
    TRACE_LIST = "trace.list"
    TRACE_GET = "trace.get"
    # ---- Server -> Client ----
    WELCOME = "welcome"
    SESSION_CREATED = "session.created"
    ATTACHED = "attached"
    DETACHED = "detached"
    SESSION_LIST_RESP = "session_list"
    EVENT = "event"
    REPLAY_START = "replay_start"
    REPLAY_END = "replay_end"
    ASK = "ask"  # 服务端请求：{id, question}
    SHOW_QUESTIONS = "show_questions"
    SHOW_PLAN = "show_plan"
    SHOW_SKILLS = "show_skills"
    SHOW_AGENTS = "show_agents"
    NOTIFY = "notify"
    USAGE = "usage"
    CLOSE = "close"
    ERROR = "error"
    TRACE_LIST_RESP = "trace_list"
    TRACE_TREE = "trace_tree"


@runtime_checkable
class WsConnection(Protocol):
    """WebSocket 连接的最小接口（server / client 共用，屏蔽 websockets 版本差异）。

    只要求能 ``send`` 字符串消息并支持 ``async for`` 读取字符串消息。
    """

    async def send(self, message: str) -> None: ...

    def __aiter__(self) -> AsyncIterator[Any]:
        # 异步迭代产出消息（daemon 走文本模式为 str；websockets 也可能在其它模式产出 bytes，
        # 故此处用 Any 以兼容不同连接实现与测试替身）。
        ...


def make_message(
    type: MsgType | str,
    payload: dict[str, Any] | None = None,
    *,
    id: str | None = None,
    session: str | None = None,
) -> str:
    """构造一条协议消息（序列化为 JSON 字符串）。"""
    t = type.value if isinstance(type, MsgType) else type
    msg: dict[str, Any] = {"type": t, "payload": payload or {}}
    if id is not None:
        msg["id"] = id
    if session is not None:
        msg["session"] = session
    return json.dumps(msg, ensure_ascii=False)


def parse_message(raw: str) -> dict[str, Any]:
    """解析一条协议消息（JSON 字符串 -> dict）。"""
    return json.loads(raw)
