"""M8 全屏 TUI 测试：基于 Textual `run_test` / `Pilot` 做 headless 测试（无需真 TTY）。

- 不依赖旧 `TerminalTransport` / `FakeModel` 渲染路径，CI 可跑。
- 旧 `CliRunner` 单测继续走 `TerminalTransport`（见 `test_cli.py`），不受影响。
"""

from __future__ import annotations

from agent.core.events import Event, EventStream, EventType
from agent.core.model import Decision, FakeModel, ToolCall
from agent.core.session import Session
from agent.config.settings import Settings
from agent.runtime.registry import ToolRegistry, ToolResult, tool
from agent.tui.app import ChatApp, _StaticLine
from agent.tui.widgets import AssistantMessage, ToolBlock, UserMessage
from agent.runtime.textual_transport import TextualTransport
from textual.widgets import TextArea
import asyncio


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

