"""M7.3 CLI 客户端测试：用假 ws 连接验证渲染复用 + HITL 回传。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from agent.core.intent import Question
from agent.daemon.client import _parse_command, _run
from agent.daemon.protocol import MsgType, make_message, parse_message
from agent.runtime.terminal_transport import TerminalTransport


class FakeWS:
    """可注入消息序列的假 ws 连接（支持 async for + send）。"""

    def __init__(self, incoming):
        self._in = list(incoming)
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._in:
            return self._in.pop(0)
        raise StopAsyncIteration

    async def send(self, msg):
        self.sent.append(msg)

    async def close(self):
        self.closed = True


def _types(ws):
    return [parse_message(m)["type"] for m in ws.sent]


async def test_client_answers_ask_and_sends_task():
    ws = FakeWS([
        make_message(MsgType.WELCOME),
        make_message(MsgType.SESSION_CREATED, {"session_id": "s1"}),
        make_message(MsgType.ASK, {"id": "r1", "question": Question("choose", options=["a", "b"]).to_dict()}, id="r1"),
        make_message(MsgType.CLOSE),
    ])
    transport = TerminalTransport(interactive=False, context_mgr=None)
    transport.ask = AsyncMock(return_value="a")  # 避免真实 TTY 交互
    await _run(ws, run_task="hello", transport=transport)
    types = _types(ws)
    assert MsgType.HELLO.value in types
    assert MsgType.SESSION_NEW.value in types
    assert MsgType.TASK_SEND.value in types
    answers = [m for m in ws.sent if parse_message(m)["type"] == MsgType.ANSWER.value]
    assert answers
    assert parse_message(answers[0])["payload"]["text"] == "a"
    assert parse_message(answers[0])["id"] == "r1"


def test_parse_command():
    assert _parse_command("/plan") == ("plan", None)
    assert _parse_command("/skill foo") == ("skill", "foo")
    assert _parse_command("/agent coder fix bug") == ("agent", "coder fix bug")
    assert _parse_command("just text") == ("", None)
