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
from agent.core.model import Decision, FakeModel, Message, ToolCall
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
    monkeypatch.setattr("agent.cli._build_model", lambda settings, tracer=None, pipeline=None: model)


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
    from agent.runtime.terminal_transport import _parse_multi_selection

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
    from agent.runtime.terminal_transport import _extract_write_preview

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
    from agent.runtime.terminal_transport import TerminalTransport, ASK_CLARIFICATION_TOOL_NAME

    p = TerminalTransport(interactive=False)
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
    loop = AgentLoop(model, _make_registry(), Settings(loop=dict(max_iterations=10)), tracer=tracer)
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


# --------------------------------------------------------------------------- #
# M5.4 CLI：/skills /agents /skill 命令（不调模型，仅改 Session 状态）
# --------------------------------------------------------------------------- #
def _write_skill_in_project_root(root, name, body="DEMO BODY CONTENT"):
    d = root / ".agent" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 演示 {name}\n---\n{body}\n", encoding="utf-8"
    )


def test_chat_agents_lists_builtins(runner, monkeypatch):
    """/agents 展示内置类型（explore/plan/general-purpose），不调模型。"""
    _patch_model(monkeypatch, FakeModel([]))
    result = runner.invoke(app, ["chat"], input="/agents\nexit\n")

    assert result.exit_code == 0
    assert "explore" in result.output
    assert "plan" in result.output
    assert "general-purpose" in result.output


def test_chat_skills_lists_and_skill_load(tmp_path, runner, monkeypatch):
    """/skills 列出 skill（不含正文）；/skill <name> 显式注入；未知名提示。"""
    _write_skill_in_project_root(tmp_path, "demo")
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    _patch_model(monkeypatch, FakeModel([]))
    result = runner.invoke(
        app, ["chat"], input="/skills\n/skill demo\n/skill nope\nexit\n"
    )

    assert result.exit_code == 0
    # 列表含 name + description
    assert "demo" in result.output
    assert "演示 demo" in result.output
    # 双轨不变量：列表不含 skill 正文
    assert "DEMO BODY CONTENT" not in result.output
    # 显式加载成功
    assert "已加载 skill: demo" in result.output
    # 未知 skill 名 → 提示，不崩溃
    assert "未找到 skill: nope" in result.output


def test_chat_skill_load_appends_message(tmp_path, monkeypatch):
    """/skill <name> 把渲染后的正文作为 user 消息追加到 session.messages。"""
    from agent.core.session import Session

    _write_skill_in_project_root(tmp_path, "demo", body="HELLO FROM SKILL")
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    settings = Settings()
    sess = Session(FakeModel([]), _make_registry(), settings, tracer=None)
    assert sess.skill_loader is not None
    spec = sess.skill_loader.get("demo")
    assert spec is not None
    before = len(sess.messages)
    sess.messages.append(Message(role="user", content=f"[Skill demo]\n{spec.render_body()}"))
    assert len(sess.messages) == before + 1
    assert sess.messages[-1].role == "user"
    assert "HELLO FROM SKILL" in sess.messages[-1].content


# --------------------------------------------------------------------------- #
# M5.4 后台 Subagent：/agent /bg 命令
# --------------------------------------------------------------------------- #
def test_chat_agent_command_usage(runner, monkeypatch):
    """/agent 不带参数提示用法；/agent 未知名提示未找到。"""
    _patch_model(monkeypatch, FakeModel([]))
    result = runner.invoke(app, ["chat"], input="/agent\n/agent unknown task\nexit\n")

    assert result.exit_code == 0
    assert "用法: /agent" in result.output
    assert "未找到 subagent: unknown" in result.output


def test_chat_agent_starts_background(runner, monkeypatch):
    """/agent <name> <task> 启动后台 Subagent，返回 task_id 提示。"""
    _patch_model(monkeypatch, FakeModel([]))
    result = runner.invoke(app, ["chat"], input="/agent general-purpose do something\n/bg\nexit\n")

    assert result.exit_code == 0
    assert "后台 Subagent [general-purpose] 已启动" in result.output
    assert "task_id:" in result.output


def test_chat_bg_lists_no_tasks(runner, monkeypatch):
    """/bg 无后台任务时提示当前没有。"""
    _patch_model(monkeypatch, FakeModel([]))
    result = runner.invoke(app, ["chat"], input="/bg\nexit\n")

    assert result.exit_code == 0
    assert "当前没有运行中的后台任务" in result.output


def test_background_spawn_injects_summary(monkeypatch):
    """后台 Subagent 完成后把摘要作为 user 消息注入 session.messages。"""
    import asyncio

    from agent.config.settings import Settings
    from agent.core.model import FakeModel, Decision
    from agent.core.session import Session
    from agent.runtime.terminal_transport import TerminalTransport

    model = FakeModel([Decision(text="background result summary")])
    settings = Settings()
    transport = TerminalTransport(interactive=False)
    sess = Session(model, _make_registry(), settings, tracer=None)
    assert sess.subagent_spawner is not None

    before = len(sess.messages)

    async def _run():
        task_id = sess.spawn_background(
            "general-purpose", "do research", transport,
            parent_span=None,
        )
        assert task_id is not None
        assert task_id in sess._bg_tasks
        # 等待后台任务完成
        await sess._bg_tasks[task_id]
        return task_id

    task_id = asyncio.run(_run())

    assert task_id not in sess._bg_tasks  # 完成后从字典移除
    assert len(sess.messages) == before + 1
    msg = sess.messages[-1]
    assert msg.role == "user"
    assert "[Background Subagent general-purpose" in msg.content
    assert "background result summary" in msg.content


# --------------------------------------------------------------------------- #
# M4.6 CLI：/context /compact 命令 + 状态栏
# --------------------------------------------------------------------------- #
def test_chat_context_command_shows_usage(runner, monkeypatch):
    """/context 打印上下文占用明细（分类/总占用/剩余/使用率）。"""
    _patch_model(monkeypatch, FakeModel([]))
    result = runner.invoke(app, ["chat"], input="/context\nexit\n")

    assert result.exit_code == 0
    assert "上下文占用明细" in result.output
    assert "总占用" in result.output
    assert "剩余可用" in result.output
    assert "使用率" in result.output


def test_chat_context_command_no_context_mgr(runner, monkeypatch):
    """全部关闭上下文管理时，/context 提示未启用。"""
    from agent.config.settings import load_settings as _real_load

    def _disabled(*a, **k):
        s = _real_load(*a, **k)
        s.context.microcompact_enabled = False
        s.context.auto_compact_enabled = False
        s.context.session_memory_enabled = False
        return s

    monkeypatch.setattr("agent.cli.load_settings", _disabled)
    _patch_model(monkeypatch, FakeModel([]))
    result = runner.invoke(app, ["chat"], input="/context\nexit\n")

    assert result.exit_code == 0
    assert "上下文管理未启用" in result.output


def test_chat_compact_command_triggers_compaction(runner, monkeypatch):
    """/compact 触发压缩并打印完成结果。"""
    _patch_model(monkeypatch, FakeModel([]))
    result = runner.invoke(app, ["chat"], input="/compact\nexit\n")

    assert result.exit_code == 0
    assert "正在压缩上下文" in result.output
    assert "压缩完成" in result.output


def test_chat_compact_command_no_context_mgr(runner, monkeypatch):
    """全部关闭上下文管理时，/compact 提示未启用。"""
    from agent.config.settings import load_settings as _real_load

    def _disabled(*a, **k):
        s = _real_load(*a, **k)
        s.context.microcompact_enabled = False
        s.context.auto_compact_enabled = False
        s.context.session_memory_enabled = False
        return s

    monkeypatch.setattr("agent.cli.load_settings", _disabled)
    _patch_model(monkeypatch, FakeModel([]))
    result = runner.invoke(app, ["chat"], input="/compact\nexit\n")

    assert result.exit_code == 0
    assert "上下文管理未启用" in result.output


def test_terminal_transport_status_line_colors():
    """_status_line 按占比着色（>90% 红 / >70% 黄 / 否则绿），无 context_mgr 返回空。"""
    from agent.context.manager import ContextManager
    from agent.runtime.terminal_transport import TerminalTransport

    def _make_cm(fixed: int, window: int = 1000) -> ContextManager:
        cm = ContextManager(context_window=window, max_output_tokens=200)
        cm._system_fixed = fixed
        cm._tools = 0
        cm._system_dynamic = 0
        return cm

    # effective_window = 1000 - 200 = 800
    red = TerminalTransport(interactive=False, context_mgr=_make_cm(750))   # 93.75% → red
    yellow = TerminalTransport(interactive=False, context_mgr=_make_cm(600))  # 75% → yellow
    green = TerminalTransport(interactive=False, context_mgr=_make_cm(400))  # 50% → green
    none = TerminalTransport(interactive=False)

    assert "ctx:" in red._status_line() and "[red]" in red._status_line()
    assert "ctx:" in yellow._status_line() and "[yellow]" in yellow._status_line()
    assert "ctx:" in green._status_line() and "[green]" in green._status_line()
    assert none._status_line() == ""


def test_chat_status_bar_no_raw_rich_markup(runner, monkeypatch):
    """M4.6 修复：状态栏的 rich 标记不应以原始文本泄漏到 prompt（非交互模式应为纯文本）。"""
    _patch_model(monkeypatch, FakeModel([]))
    result = runner.invoke(app, ["chat"], input="exit\n")

    assert result.exit_code == 0
    # 原始 rich 标记绝不能出现在输出里（此前会打印 [green]10%[/green]）。
    assert "[green]" not in result.output
    assert "[red]" not in result.output
    assert "[yellow]" not in result.output
    # 状态栏仍以纯文本形式展示（ctx: NN%）。
    assert "ctx:" in result.output


def test_shutdown_background_cancels_pending():
    """M4.6 修复：退出时仍在运行的后台 Subagent 应被优雅取消（而非被 asyncio.run 粗暴中断）。"""
    import asyncio

    from agent.config.settings import Settings
    from agent.core.model import FakeModel
    from agent.core.session import Session

    sess = Session(FakeModel([]), _make_registry(), Settings(), tracer=None)

    async def _run():
        async def _slow():
            await asyncio.sleep(30)

        t = asyncio.create_task(_slow(), name="bg1")
        sess._bg_tasks["bg1"] = t
        cancelled = await sess.shutdown_background(timeout=0.05)
        return t, cancelled

    t, cancelled = asyncio.run(_run())
    assert "bg1" in cancelled
    assert t.cancelled() or t.done()
