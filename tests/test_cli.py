"""M1.6 验收：CLI 入口 + 最简可观测。

覆盖（详见 milestones/M1-骨架/1.6-CLI入口与最简可观测.md）：
- `run "<task>"`：FakeModel 下跑通，退出码 0，输出含最终文本与最简 trace。
- `--plan`：FakeModel 先产出计划（AgentResult.plan 非空），CLI 打印计划，`--yes` 后进入执行；退出码 0。
- 模型 ask_clarification：非交互（CliRunner，无 TTY）默认报错退出（code 2，不静默跳过）。
- trace 体现父子关系：tool.exec 的 parent 是 agent.run。
- 全程用 CliRunner + 注入 FakeModel，不依赖真实 LLM。
"""

import asyncio

import pytest
from typer.testing import CliRunner

from agent.cli import _build_model, app
from agent.config.settings import Settings
from agent.core.control_tools import (
    ASK_CLARIFICATION_TOOL_NAME,
    PRESENT_PLAN_TOOL_NAME,
)
from agent.core.loop import AgentLoop
from agent.core.model import Decision, FakeModel, ToolCall
from agent.obs.tracer import Tracer
from agent.runtime.registry import ToolRegistry, ToolResult, tool


async def _echo(args: dict) -> ToolResult:
    return ToolResult(ok=True, output=str(args))


def _make_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(tool("echo", risk="read")(_echo))
    return r


@pytest.fixture
def runner():
    return CliRunner()


def _patch_model(monkeypatch, model: FakeModel):
    monkeypatch.setattr("agent.cli._build_model", lambda settings: model)


def test_run_basic_prints_final_and_trace(runner, monkeypatch):
    _patch_model(monkeypatch, FakeModel([Decision(text="hello world")]))
    result = runner.invoke(app, ["run", "do something"])

    assert result.exit_code == 0
    assert "hello world" in result.stdout
    assert "Trace" in result.stdout  # Panel 标题（美化后的 trace 输出）
    assert "agent.run" in result.stdout


def test_run_with_plan_flag(runner, monkeypatch):
    model = FakeModel([
        Decision(tool_calls=[ToolCall(id="p", name=PRESENT_PLAN_TOOL_NAME,
                                      arguments={"body": "B", "steps": [{"id": "S1", "title": "t"}]})]),
        Decision(text="executed"),
    ])
    _patch_model(monkeypatch, model)
    result = runner.invoke(app, ["run", "plan it", "--plan", "--yes"])

    assert result.exit_code == 0
    assert "Plan" in result.stdout
    assert "executed" in result.stdout


def test_run_clarification_non_interactive_errors(runner, monkeypatch):
    model = FakeModel([Decision(tool_calls=[ToolCall(
        id="cq", name=ASK_CLARIFICATION_TOOL_NAME,
        arguments={"questions": [{"question": "用哪个框架？"}]},
    )])])
    _patch_model(monkeypatch, model)
    result = runner.invoke(app, ["run", "vague"])

    assert result.exit_code == 2  # 非交互：报错退出（不静默跳过）
    assert "用哪个框架" in result.output  # 问题被打印出来（合并输出）


def test_trace_parent_child():
    """tool.exec 的 parent 必须是 agent.run（M1.6 验收：trace 体现父子关系）。"""
    tracer = Tracer()
    model = FakeModel([
        Decision(tool_calls=[ToolCall(id="t1", name="echo", arguments={"x": 1})]),
        Decision(text="done"),
    ])
    loop = AgentLoop(model, _make_registry(), Settings(max_iterations=10), tracer=tracer)
    asyncio.run(loop.run("task"))

    roots = [s for s in tracer.spans if s.parent_id is None]
    assert len(roots) == 1 and roots[0].name == "agent.run"
    agent_span = roots[0]
    tool_spans = [s for s in tracer.spans if s.name == "tool.exec"]
    assert tool_spans, "应有 tool.exec span"
    assert all(s.parent_id == agent_span.id for s in tool_spans)


def test_chat_mode_switch_commands(runner, monkeypatch):
    """chat 中任意轮次用 /plan /exec /mode 自由切换模式（不触发模型）。"""
    _patch_model(monkeypatch, FakeModel([]))
    result = runner.invoke(app, ["chat"], input="/plan\n/mode\n/exec\n/mode\nexit\n")

    assert result.exit_code == 0
    assert "已切换到 PLAN 模式" in result.output
    assert "当前模式：PLAN" in result.output
    assert "已切换到 EXEC 模式" in result.output
    assert "当前模式：EXEC" in result.output


def test_chat_plan_then_approve_then_exec(runner, monkeypatch):
    """chat：先 /plan 产出计划，confirm 批准后切到 EXEC 执行同一任务。"""
    model = FakeModel([
        Decision(tool_calls=[ToolCall(id="p", name=PRESENT_PLAN_TOOL_NAME,
                                      arguments={"body": "计划B", "steps": [{"id": "S1", "title": "t"}]})]),
        Decision(text="executed"),
    ])
    _patch_model(monkeypatch, model)
    result = runner.invoke(app, ["chat"], input="/plan\ndesign\ny\nexit\n")

    assert result.exit_code == 0
    assert "计划B" in result.output          # 计划被打印
    assert "executed" in result.output       # 批准后进入 EXEC 执行
