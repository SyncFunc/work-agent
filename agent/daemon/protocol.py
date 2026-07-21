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
  ``approve`` / ``command``
- Server → Client：``welcome`` / ``session.created`` / ``attached`` / ``detached`` /
  ``session_list`` / ``event`` / ``replay_start`` / ``replay_end`` / ``ask`` /
  ``show_questions`` / ``show_plan`` / ``show_skills`` / ``show_agents`` / ``notify`` /
  ``usage`` / ``close`` / ``error``

事件 ``event`` 直接承载 ``Event.to_dict()``，是天然序列化边界（见 M7.2 知识沉淀）。
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

DAEMON_VERSION = "0.1.0"
PROTOCOL_VERSION = "1.0"


class MsgType(str, Enum):
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
    APPROVE = "approve"            # 客户端回传：{id, approved}
    COMMAND = "command"
    # ---- Server -> Client ----
    WELCOME = "welcome"
    SESSION_CREATED = "session.created"
    ATTACHED = "attached"
    DETACHED = "detached"
    SESSION_LIST_RESP = "session_list"
    EVENT = "event"
    REPLAY_START = "replay_start"
    REPLAY_END = "replay_end"
    ASK = "ask"                    # 服务端请求：{id, question}
    SHOW_QUESTIONS = "show_questions"
    SHOW_PLAN = "show_plan"
    SHOW_SKILLS = "show_skills"
    SHOW_AGENTS = "show_agents"
    NOTIFY = "notify"
    USAGE = "usage"
    CLOSE = "close"
    ERROR = "error"


def make_message(
    type: "MsgType | str",
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
