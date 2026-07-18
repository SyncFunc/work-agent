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


def test_parse_multi_selection_by_index_and_label():
    """回归：多选解析支持「编号(逗号分隔)」与「标签(逗号分隔)」，并去重保序。

    对应修复：_ptk_multi_choice 弃用 Application+CheckboxList（TTY 下不渲染选项且
    卡死），改为编号列表 + PromptSession；本函数是其纯解析核心，可单测。
    """
    from agent.cli import _parse_multi_selection

    opts = ["加", "减", "乘", "除"]
    assert _parse_multi_selection("1,3", opts) == ["加", "乘"]
    assert _parse_multi_selection("乘, 除", opts) == ["乘", "除"]
    assert _parse_multi_selection("1, 1, 3", opts) == ["加", "乘"]        # 去重保序
    assert _parse_multi_selection("0, 99, x", opts) == []                # 越界/无效忽略
    assert _parse_multi_selection("", opts) == []                        # 直接回车=不选
    assert _parse_multi_selection("2,减", opts) == ["减"]  # 编号与标签混用，末尾去重


def test_extract_write_preview_from_partial_json():
    """回归：write/edit 流式预览能从「可能不完整的」参数 JSON 中提取正文片段。

    对应修复：模型流式生成 write 的 content 时，on_tool_call_delta 用本函数实时预览，
    避免大段写入时终端长时间无输出。
    """
    from agent.cli import _extract_write_preview

    assert _extract_write_preview('{"path":"a.py","content":"print(1)') == "print(1)"
    # 模型流式产出的参数里换行是 JSON 转义的 \n（两字符），函数按原文返回字面量
    assert _extract_write_preview('{"path":"a.py","content":"line1\\nline2"') == "line1\\nline2"
    # 未完成值带尾随引号时被去掉
    assert _extract_write_preview('{"content":"abc"') == "abc"
    # edit 用 new_string
    assert _extract_write_preview('{"path":"a.py","new_string":"hello"}') == "hello"
    # 尚未流到正文 → 空
    assert _extract_write_preview('{"path":"a.py"') == ""
    assert _extract_write_preview("") == ""




def test_on_tool_call_delta_skips_ask_clarification_live():
    """回归：ask_clarification 是控制工具，其流式预览**不应**创建 tool-live。

    对应修复：澄清闸门会提前返回，on_tool_call 永不被调用，若 on_tool_call_delta 为
    ask_clarification 创建 Live，该 Live 不被收尾，残留面板会扰乱澄清面板渲染（表现：
    重复 ask_clarification 面板、澄清选项不显示）。write 等真实工具仍应创建预览 Live。
    """
    from agent.cli import _RichPresenter, ASK_CLARIFICATION_TOOL_NAME

    p = _RichPresenter()
    try:
        p.on_tool_call_delta(0, ASK_CLARIFICATION_TOOL_NAME, '{"questions": [{"question": "x"}]}')
        assert p._tool_live is None  # 关键：ask_clarification 不创建预览 Live
        p.on_tool_call_delta(0, "write", '{"path": "a.py", "content": "print(1)"}')
        assert p._tool_live is not None  # 真实工具仍创建预览 Live
    finally:
        p.close()


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
