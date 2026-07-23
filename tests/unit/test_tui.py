"""M8 全屏 TUI 测试：基于 Textual `run_test` / `Pilot` 做 headless 测试（无需真 TTY）。

- 不依赖旧 `TerminalTransport` / `FakeModel` 渲染路径，CI 可跑。
- 旧 `CliRunner` 单测继续走 `TerminalTransport`（见 `test_cli.py`），不受影响。
"""

from __future__ import annotations

from agent.core.events import Event, EventStream, EventType
from agent.core.model import ToolCall
from agent.runtime.registry import ToolResult
from agent.tui.app import ChatApp
from agent.tui.widgets import AssistantMessage, ToolBlock, UserMessage
from agent.runtime.textual_transport import TextualTransport


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

