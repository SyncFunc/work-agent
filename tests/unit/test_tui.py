"""M8 全屏 TUI 测试：基于 Textual `run_test` / `Pilot` 做 headless 测试（无需真 TTY）。

- 不依赖旧 `TerminalTransport` / `FakeModel` 渲染路径，CI 可跑。
- 旧 `CliRunner` 单测继续走 `TerminalTransport`（见 `test_cli.py`），不受影响。
"""

from __future__ import annotations

from agent.tui.app import ChatApp


async def test_app_boots():
    """M8.0：空 ChatApp 能 headless 启动并挂载主区 RichLog。"""
    async with ChatApp().run_test() as pilot:
        assert pilot.app is not None
        assert pilot.app.query_one("#log") is not None
        assert pilot.app.query_one("#input") is not None
