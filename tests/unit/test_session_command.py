"""M7.5 dispatch_command 测试：确保命令语义单一来源（进程内与 daemon 共用）。"""

from __future__ import annotations

from types import SimpleNamespace

from agent.config.settings import load_settings
from agent.core.session_command import dispatch_command
from agent.runtime.terminal_transport import TerminalTransport


class FakeSession:
    def __init__(self):
        self.plan_mode = False
        self.plan_path = None
        self.context_mgr = None
        self.skill_loader = None
        self.subagent_spawner = None
        self.messages: list = []
        self.loop = SimpleNamespace(_agent_span=None)
        self.settings = load_settings()

    def list_skills(self):
        return []

    def list_agents(self):
        return []

    def list_background_tasks(self):
        return []

    def spawn_background(self, *a, **k):
        return None

    async def step(self, *a, **k):
        return (None, None)


async def test_dispatch_plan_and_mode():
    s = FakeSession()
    t = TerminalTransport(interactive=False)
    settings = load_settings()
    assert await dispatch_command(s, "/plan", t, settings) is True
    assert s.plan_mode is True
    assert await dispatch_command(s, "/mode", t, settings) is True


async def test_dispatch_unknown_slash_is_handled():
    s = FakeSession()
    t = TerminalTransport(interactive=False)
    settings = load_settings()
    # 未知 slash 命令应返回 True（避免误当任务发往模型），并通过 feedback 提示。
    assert await dispatch_command(s, "/nope", t, settings) is True


async def test_dispatch_non_slash_returns_false():
    s = FakeSession()
    t = TerminalTransport(interactive=False)
    settings = load_settings()
    assert await dispatch_command(s, "do something", t, settings) is False


async def test_dispatch_help_lists_commands():
    s = FakeSession()
    t = TerminalTransport(interactive=False)
    settings = load_settings()
    captured: list[str] = []

    def feedback(msg: str) -> None:
        captured.append(msg)

    # 裸 / 与 /help 都应展示命令清单
    assert await dispatch_command(s, "/", t, settings, feedback=feedback) is True
    assert await dispatch_command(s, "/help", t, settings, feedback=feedback) is True
    text = "\n".join(captured)
    assert "可用命令" in text
    for cmd in ("/plan", "/exec", "/context", "/compact", "/skills", "/agents", "/help"):
        assert cmd in text
