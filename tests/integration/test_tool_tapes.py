"""M6.3 Tier2 工具录像带（tool tapes）：用 ``RecordedModel`` 重放录制的 Decision 序列。

验证工具调用顺序 / 参数 / 错误分支（未知工具优雅降级），确定、零成本、不调真实 LLM。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.core.loop import AgentLoop
from agent.core.model import Decision, FakeModel, ToolCall
from agent.testing.recorded_model import (
    RecordedModel,
    decisions_from_eventstream,
    dump_tape,
)
from tests.conftest import _make_registry, _settings


def test_tool_tape_replays_order_and_params():
    """录像带重放：两个 read 调用按 r1→r2 顺序执行，tool_result 按 id 配对回传。"""
    tape = [
        Decision(
            tool_calls=[
                ToolCall(id="r1", name="read", arguments={"path": "a.txt"}),
                ToolCall(id="r2", name="read", arguments={"path": "b.txt"}),
            ]
        ),
        Decision(text="done"),
    ]
    model = RecordedModel(tape)
    loop = AgentLoop(model, _make_registry(), _settings())
    result = asyncio.run(loop.run("read two files"))

    uses = [e.tool_use for e in result.events if e.type == "tool_use"]
    assert [u.id for u in uses] == ["r1", "r2"]
    results = [e for e in result.events if e.type == "tool_result"]
    assert results[0].tool_call_id == "r1"
    assert results[1].tool_call_id == "r2"
    # 第二次决策拿到 tool 结果并产出最终答案
    assert result.messages[-1].role == "assistant"
    assert result.messages[-1].content == "done"


def test_tool_tape_error_branch_graceful_degradation():
    """错误分支：未知工具被降级为 ToolResult(ok=False)，模型下一轮收到错误并恢复（不崩溃）。"""
    tape = [
        Decision(tool_calls=[ToolCall(id="g1", name="ghost", arguments={})]),
        Decision(text="recovered"),
    ]
    model = RecordedModel(tape)
    loop = AgentLoop(model, _make_registry(), _settings())
    result = asyncio.run(loop.run("call unknown"))

    tr = next(e for e in result.events if e.type == "tool_result")
    assert not tr.tool_result.ok
    assert "unknown tool" in (tr.tool_result.error or "")
    # 模型在下一轮收到该错误并已优雅恢复（降级而非崩溃）
    assert result.text == "recovered"


def test_real_tape_file_replays():
    """真实 tape 文件回放：从 ``fixtures/tapes`` 加载录制产物，RecordedModel 回放，
    工具调用顺序 / 参数 / 最终文本与录制时一致（零 LLM、零网络）。"""
    tape_path = Path(__file__).parent / "fixtures" / "tapes" / "echo_one.json"
    model = RecordedModel.from_tape(tape_path)
    result = asyncio.run(AgentLoop(model, _make_registry(), _settings()).run("replay"))

    uses = [e.tool_use for e in result.events if e.type == "tool_use"]
    assert [u.name for u in uses] == ["echo"]
    assert uses[0].arguments == {"msg": "hello"}
    assert result.text == "done"


def test_record_replay_roundtrip(tmp_path):
    """录制闭环全链路：用 FakeModel 真实跑一轮 loop → 从 EventStream 提取决策 →
    落盘 tape → 重新加载 → RecordedModel 回放 → 行为完全一致（工具顺序/参数/最终文本）。

    证明「录制→落盘→回放」链路可工作且回放结果与原运行等价；该链路不依赖真实 LLM，
    nightly 真实 LLM 录制时走的是同一套 ``decisions_from_eventstream`` / ``dump_tape``。
    """
    script = [
        Decision(tool_calls=[ToolCall(id="t1", name="echo", arguments={"msg": "rec"})]),
        Decision(text="final answer"),
    ]
    loop = AgentLoop(FakeModel(script), _make_registry(), _settings())
    result = asyncio.run(loop.run("do something"))

    decisions = decisions_from_eventstream(result.events)
    assert decisions  # 至少含初次决策
    tape_path = tmp_path / "replay.json"
    dump_tape(decisions, tape_path)

    replay = asyncio.run(
        AgentLoop(RecordedModel.from_tape(tape_path), _make_registry(), _settings()).run(
            "do something"
        )
    )
    orig = [e.tool_use for e in result.events if e.type == "tool_use"]
    new = [e.tool_use for e in replay.events if e.type == "tool_use"]
    assert [(t.name, t.arguments) for t in orig] == [(t.name, t.arguments) for t in new]
    assert replay.text == result.text
