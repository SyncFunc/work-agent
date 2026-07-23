"""M6.3 录制闭环 nightly 验证（slow）：自动落盘 + 回放等价。

默认 ``pytest -q`` 跳过（addopts=-m 'not slow'），``pytest -m slow`` 单独跑。

这里用 ``FakeModel`` 代表「真实运行一轮」——它走的是**完整 Session 链路**（含
USER / DECISION / TOOL_USE / TOOL_RESULT / FINAL 全部事件），比 integration 的
``AgentLoop`` 直跑更贴近真实运行轨迹。接真实 LLM 时，同一段 ``dump_tape`` 逻辑
产出的就是**真实录像**（结构完全一致，只是决策来自真实模型）。

验证点：跑完自动落盘 tape → ``RecordedModel.from_tape`` 回放 → 工具顺序/参数/
最终文本与原运行等价，且 tape 文件确实生成。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from agent.core.events import EventType
from agent.core.model import Decision, FakeModel, ToolCall
from agent.core.session import Session
from agent.runtime.terminal_transport import TerminalTransport
from agent.testing.recorded_model import (
    RecordedModel,
    decisions_from_eventstream,
    dump_tape,
)
from tests.conftest import _make_registry, _settings


@pytest.mark.slow
def test_record_replay_autosaved_in_session_loop(tmp_path):
    """nightly 自动落盘闭环：真实跑一轮（FakeModel 代表）→ 落盘 → 回放等价。"""
    script = [
        Decision(tool_calls=[ToolCall(id="t1", name="echo", arguments={"msg": "nightly"})]),
        Decision(text="done"),
    ]
    settings = _settings()
    session = Session(
        FakeModel(script), _make_registry(), settings, tracer=None, session_id=uuid.uuid4().hex
    )
    res, _ = asyncio.run(session.step("task", TerminalTransport(interactive=False)))

    tape = tmp_path / "nightly.json"
    dump_tape(decisions_from_eventstream(res.events), tape)
    assert tape.exists()  # 自动落盘成功

    replay = Session(
        RecordedModel.from_tape(tape),
        _make_registry(),
        settings,
        tracer=None,
        session_id=uuid.uuid4().hex,
    )
    res2, _ = asyncio.run(replay.step("task", TerminalTransport(interactive=False)))

    orig = [e.tool_use for e in res.events if e.type == EventType.TOOL_USE]
    new = [e.tool_use for e in res2.events if e.type == EventType.TOOL_USE]
    assert [(t.name, t.arguments) for t in orig] == [(t.name, t.arguments) for t in new]
    assert res2.text == res.text
