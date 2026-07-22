"""M6.3 Tier2 工具录像带（tool tapes）：用 ``RecordedModel`` 重放录制的 Decision 序列。

验证工具调用顺序 / 参数 / 错误分支（未知工具优雅降级），确定、零成本、不调真实 LLM。
"""

from __future__ import annotations

import asyncio

from agent.core.loop import AgentLoop
from agent.core.model import Decision, ToolCall
from agent.testing.recorded_model import RecordedModel
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
