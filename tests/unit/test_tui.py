"""M8 全屏 TUI 测试：基于 Textual `run_test` / `Pilot` 做 headless 测试（无需真 TTY）。

- 不依赖旧 `TerminalTransport` / `FakeModel` 渲染路径，CI 可跑。
- 旧 `CliRunner` 单测继续走 `TerminalTransport`（见 `test_cli.py`），不受影响。
"""

from __future__ import annotations

import asyncio
from unittest import mock

from rich.syntax import Syntax
from textual.widgets import Collapsible, TextArea

from agent.config.settings import Settings
from agent.core.events import Event, EventStream, EventType
from agent.core.intent import Question
from agent.core.model import Decision, FakeModel, ToolCall
from agent.core.session import Session
from agent.runtime.approval import Action
from agent.runtime.registry import ToolRegistry, ToolResult, tool
from agent.runtime.textual_transport import TextualTransport
from agent.tui.app import AgentCommandProvider, ChatApp, _StaticLine
from agent.tui.screens import ApproveScreen, AskScreen, PlanScreen
from agent.tui.widgets import AssistantMessage, ToolBlock, UserMessage


async def test_app_boots():
    """M8.0：空 ChatApp 能 headless 启动并挂载主区 RichLog。"""
    async with ChatApp().run_test() as pilot:
        assert pilot.app is not None
        assert pilot.app.query_one("#log") is not None
        assert pilot.app.query_one("#input") is not None


async def test_transport_maps_events():
    """M8.1：TextualTransport 订阅事件流后，各类事件映射到对应部件。"""
    async with ChatApp().run_test() as pilot:
        app = pilot.app
        t = TextualTransport(app)
        es = EventStream()
        t.bind(es)

        es.append(Event(type=EventType.USER, text="hi"))
        es.append(Event(type=EventType.TEXT, text="hello", kind="content"))
        es.append(
            Event(
                type=EventType.TOOL_USE,
                tool_use=ToolCall(id="t1", name="echo", arguments={"x": 1}),
            )
        )
        es.append(
            Event(
                type=EventType.TOOL_RESULT,
                tool_call_id="t1",
                tool_result=ToolResult(ok=True, output="out", error=None, diff=None),
            )
        )
        es.append(Event(type=EventType.DECISION))
        await pilot.pause()

        assert len(app.query(UserMessage)) >= 1
        assert len(app.query(AssistantMessage)) >= 1
        assert len(app.query(ToolBlock)) >= 1


def _make_session(model):
    async def _echo(args):
        return ToolResult(ok=True, output="done")

    reg = ToolRegistry()
    reg.register(tool("echo", risk="read")(_echo))
    return Session(model, reg, Settings(), tracer=None)


async def test_input_drives_step():
    """M8.2：TextArea 提交任务 → worker 线程跑 Session.step → 主区出现助理回复。"""
    session = _make_session(FakeModel([Decision(text="hello from agent")]))
    app = ChatApp(session=session, settings=Settings())
    async with app.run_test() as pilot:
        ta = pilot.app.query_one(TextArea)
        ta.text = "say hello"
        await pilot.press("ctrl+j")
        await pilot.pause()
        await asyncio.sleep(0.2)  # 等待 worker 线程把事件桥接回主线程渲染
        assert len(pilot.app.query(AssistantMessage)) >= 1


async def test_slash_command_dispatched():
    """M8.2：/ 命令经 dispatch_command 处理（/context 触发 notify 输出）。"""
    session = _make_session(FakeModel([]))
    app = ChatApp(session=session, settings=Settings())
    async with app.run_test() as pilot:
        ta = pilot.app.query_one(TextArea)
        ta.text = "/context"
        await pilot.press("ctrl+j")
        await pilot.pause()
        await asyncio.sleep(0.1)
        # /context 不触发模型，仅经 dispatch_command 渲染上下文占用信息（notify 行存在）
        assert len(pilot.app.query(_StaticLine)) >= 1


# --------------------------------------------------------------------------- #
# M8.3：HITL 模态屏（ModalScreen + 线程安全 Future）
# --------------------------------------------------------------------------- #
async def _wait_for_screen(pilot, cls, *, timeout: float = 5.0) -> bool:
    """轮询直到 app.screen 变为给定模态屏类型（worker 线程经 call_from_thread 推屏）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if isinstance(pilot.app.screen, cls):
            return True
        await pilot.pause()
    return False


async def _wait_for(pred, *, timeout: float = 5.0) -> bool:
    """轮询直到谓词成立。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_hitl_ask_multichoice():
    """M8.3：AskScreen 数字键选择 → ask 返回对应选项文本。"""
    async with ChatApp().run_test() as pilot:
        app = pilot.app
        t = TextualTransport(app)
        q = Question(question="选哪个?", options=["A", "B", "C"])
        task = asyncio.create_task(t.ask(q))
        await pilot.pause()
        assert isinstance(app.screen, AskScreen)
        await pilot.press("2")
        await pilot.pause()
        assert await task == "B"
        assert not isinstance(app.screen, AskScreen)


async def test_hitl_ask_freetext():
    """M8.3：无选项时 AskScreen 用 Input 自由输入，回车提交。"""
    async with ChatApp().run_test() as pilot:
        app = pilot.app
        t = TextualTransport(app)
        q = Question(question="你叫什么?")
        task = asyncio.create_task(t.ask(q))
        await pilot.pause()
        assert isinstance(app.screen, AskScreen)
        await pilot.press("a", "l", "i", "c", "e")
        await pilot.press("enter")
        await pilot.pause()
        assert await task == "alice"
        assert not isinstance(app.screen, AskScreen)


async def test_hitl_approve():
    """M8.3：ApproveScreen 按 y → approve 返回 True。"""
    async with ChatApp().run_test() as pilot:
        app = pilot.app
        t = TextualTransport(app)
        action = Action(tool="bash", risk="exec", args={"cmd": "rm -rf /"}, description="危险命令")
        task = asyncio.create_task(t.approve(action))
        await pilot.pause()
        assert isinstance(app.screen, ApproveScreen)
        await pilot.press("y")
        await pilot.pause()
        assert await task is True
        assert not isinstance(app.screen, ApproveScreen)


async def test_hitl_reject():
    """M8.3：ApproveScreen 按 n → approve 返回 False。"""
    async with ChatApp().run_test() as pilot:
        app = pilot.app
        t = TextualTransport(app)
        action = Action(tool="bash", risk="exec", args={"cmd": "rm -rf /"}, description="危险命令")
        task = asyncio.create_task(t.approve(action))
        await pilot.pause()
        assert isinstance(app.screen, ApproveScreen)
        await pilot.press("n")
        await pilot.pause()
        assert await task is False
        assert not isinstance(app.screen, ApproveScreen)


async def test_hitl_plan_confirm():
    """M8.3：PlanScreen 按 y → confirm_plan 返回 True。"""
    async with ChatApp().run_test() as pilot:
        app = pilot.app
        t = TextualTransport(app)
        task = asyncio.create_task(t.confirm_plan())
        await pilot.pause()
        assert isinstance(app.screen, PlanScreen)
        await pilot.press("y")
        await pilot.pause()
        assert await task is True
        assert not isinstance(app.screen, PlanScreen)


async def test_hitl_plan_decline():
    """M8.3：PlanScreen 按 n → confirm_plan 返回 False。"""
    async with ChatApp().run_test() as pilot:
        app = pilot.app
        t = TextualTransport(app)
        task = asyncio.create_task(t.confirm_plan())
        await pilot.pause()
        assert isinstance(app.screen, PlanScreen)
        await pilot.press("n")
        await pilot.pause()
        assert await task is False
        assert not isinstance(app.screen, PlanScreen)


def _make_approval_session(model, *, mode: str = "on-request") -> Session:
    """构造会在执行前触发审批门的会话（on-request 模式 + approval_request=True）。

    注意：用 ``risk="exec"`` 注册 echo 工具——gate 对 ``read`` 风险有「只读自动放行」
    的早返回分支，会先于 ``approval_request`` 判定，导致永不弹审批框。
    """

    async def _echo(args):
        return ToolResult(ok=True, output="done")

    reg = ToolRegistry()
    reg.register(tool("echo", risk="exec")(_echo))
    settings = Settings()
    settings.approval.mode = mode
    return Session(model, reg, settings, tracer=None)


async def test_hitl_approve_e2e():
    """M8.3：worker 线程触发审批门 → ApproveScreen → 按 y 放行 → 会话继续产出最终答案。"""
    model = FakeModel(
        [
            Decision(
                tool_calls=[
                    ToolCall(id="t1", name="echo", arguments={"x": 1}, approval_request=True)
                ]
            ),
            Decision(text="approved path"),
        ]
    )
    session = _make_approval_session(model)
    app = ChatApp(session=session, settings=session.settings)
    async with app.run_test() as pilot:
        ta = pilot.app.query_one(TextArea)
        ta.text = "do echo"
        await pilot.press("ctrl+j")
        assert await _wait_for_screen(pilot, ApproveScreen)
        await pilot.press("y")
        assert await _wait_for(lambda: len(pilot.app.query(AssistantMessage)) >= 1, timeout=5.0)
        # 放行后工具执行 + 下一轮最终答案都已流式渲染
        assert len(pilot.app.query(AssistantMessage)) >= 1


async def test_hitl_reject_e2e():
    """M8.3：worker 线程触发审批门 → ApproveScreen → 按 n 拒绝 → 会话进入拒绝分支继续。"""
    model = FakeModel(
        [
            Decision(
                tool_calls=[
                    ToolCall(id="t1", name="echo", arguments={"x": 1}, approval_request=True)
                ]
            ),
            Decision(text="rejected path"),
        ]
    )
    session = _make_approval_session(model)
    app = ChatApp(session=session, settings=session.settings)
    async with app.run_test() as pilot:
        ta = pilot.app.query_one(TextArea)
        ta.text = "do echo"
        await pilot.press("ctrl+j")
        assert await _wait_for_screen(pilot, ApproveScreen)
        await pilot.press("n")
        assert await _wait_for(lambda: len(pilot.app.query(AssistantMessage)) >= 1, timeout=5.0)
        # 拒绝后工具返回 rejected，循环继续到下一轮最终答案，会话未中断
        assert len(pilot.app.query(AssistantMessage)) >= 1


# --------------------------------------------------------------------------- #
# M8.4：流式节流 + 可折叠工具块 + diff 高亮
# --------------------------------------------------------------------------- #
async def test_streaming_throttle():
    """M8.4：连续 50 次 TEXT 增量，Markdown.update 调用次数远小于 50（coalesce 节流生效），最终文本完整。"""
    async with ChatApp().run_test() as pilot:
        app = pilot.app
        t = TextualTransport(app)
        es = EventStream()
        t.bind(es)

        updates: list[int] = []
        orig = AssistantMessage.update

        def _count(self, renderable):
            updates.append(1)
            return orig(self, renderable)

        chunks = [f"chunk{i} " for i in range(50)]
        with mock.patch.object(AssistantMessage, "update", _count):
            for ch in chunks:
                es.append(Event(type=EventType.TEXT, text=ch, kind="content"))
            await pilot.pause()
            await asyncio.sleep(0.3)
            await pilot.pause()

        # 节流：update 次数应远小于增量次数
        assert len(updates) < 50
        # 最终文本完整
        am = app.query_one(AssistantMessage)
        assert am.full == "".join(chunks)


async def test_tool_block():
    """M8.4：TOOL_USE + TOOL_RESULT(diff) → 出现 Collapsible 工具块，结果区为 diff 高亮。"""
    async with ChatApp().run_test() as pilot:
        app = pilot.app
        t = TextualTransport(app)
        es = EventStream()
        t.bind(es)

        es.append(
            Event(
                type=EventType.TOOL_USE,
                tool_use=ToolCall(id="t1", name="write", arguments={"path": "a.py"}),
            )
        )
        es.append(
            Event(
                type=EventType.TOOL_RESULT,
                tool_call_id="t1",
                tool_result=ToolResult(
                    ok=True,
                    output="written",
                    error=None,
                    diff="--- a.py\n+++ b.py\n@@\n+print(1)\n",
                ),
            )
        )
        await pilot.pause()
        await asyncio.sleep(0.2)
        await pilot.pause()

        # 工具块以 Collapsible 形态出现（ToolBlock 继承 Collapsible）
        assert len(app.query(Collapsible)) >= 1
        block = app.query_one(ToolBlock)
        assert block._result_widget is not None
        # 结果区渲染为 diff 高亮（Syntax，lexer=diff）
        assert isinstance(block._result_body, Syntax)
        assert "print(1)" in block._result_body.code


# --------------------------------------------------------------------------- #
# M8.5：主题 + 命令面板 + 状态栏
# --------------------------------------------------------------------------- #
async def test_theme_switch():
    """M8.5：run_test 下切换主题为 catppuccin-mocha 不抛错且 app.theme 生效。"""
    async with ChatApp().run_test() as pilot:
        app = pilot.app
        # 默认从 settings.ui.theme 落回 textual-dark
        assert app.theme == "textual-dark"
        app.theme = "catppuccin-mocha"
        await pilot.pause()
        assert app.theme == "catppuccin-mocha"


async def test_command_palette():
    """M8.5：Ctrl+P 打开命令面板并注册 /compact 命令；其回调调用 _handle_command。"""
    from textual.command import CommandPalette

    async with ChatApp().run_test() as pilot:
        app = pilot.app
        app.action_open_commands()
        await pilot.pause()
        assert isinstance(app.screen, CommandPalette)

        # 提供器能搜出 /compact 命令
        provider = AgentCommandProvider(app.screen)
        hits = [h async for h in provider.search("/compact")]
        names = [h.text for h in hits]
        assert "/compact" in names

        # 调用命中命令的回调 → 经 call_later 触发 _handle_command("/compact")（spy）
        spy = mock.AsyncMock()
        with mock.patch.object(app, "_handle_command", spy):
            idx = names.index("/compact")
            hits[idx].command()
            await asyncio.sleep(0.1)
            await pilot.pause()
        spy.assert_called_with("/compact")


async def test_ctx_header():
    """M8.5：Header 副标题显示 ctx%，且随 context_mgr.estimate_usage 变化。"""
    async with ChatApp().run_test() as pilot:
        app = pilot.app
        # 注入 mock context_mgr，模拟不同用量
        cm = mock.Mock()
        cm.estimate_usage.return_value = mock.Mock(used_pct=0.42)
        app.session = mock.Mock()
        app.session.context_mgr = cm

        app._refresh_ctx()
        await pilot.pause()
        assert "ctx:" in app.sub_title
        assert "42%" in app.sub_title

        # 用量变化后刷新应更新副标题
        cm.estimate_usage.return_value = mock.Mock(used_pct=0.77)
        app._refresh_ctx()
        await pilot.pause()
        assert "77%" in app.sub_title
