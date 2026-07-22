"""M4.5 集成测试：build_context_manager / 静态动态分离 System Prompt / 固定底座 / Session & Loop 接入。

覆盖集成接缝（seams），全部用 FakeModel，不依赖真实 LLM。
"""

from __future__ import annotations

from agent.config.settings import Settings
from agent.context import build_context_manager
from agent.context.compactors.microcompact import Microcompact
from agent.core.loop import AgentLoop
from agent.core.model import Decision, FakeModel, ToolCall
from agent.core.prompts import _read_agents_md, build_system_prompt
from agent.runtime.registry import ToolRegistry, ToolResult, default_registry, tool


def _model():
    return FakeModel([Decision(text="x")])


async def test_build_context_manager_respects_switches():
    """按配置开关决定 microcompact / auto_compact 是否启用。"""
    settings = Settings(context={"microcompact_enabled": False, "auto_compact_enabled": True})
    cm = build_context_manager(settings, _model())
    assert cm.microcompact is None  # 显式禁用 → None
    assert cm.auto_compact is not None  # 启用 → 注入 AutoCompact


async def test_build_context_manager_all_disabled_yields_plain_manager():
    """auto_compact 关闭时返回 None；microcompact 仍构造默认实例（None guard 生效）。"""
    settings = Settings(context={"microcompact_enabled": True, "auto_compact_enabled": False})
    cm = build_context_manager(settings, _model())
    assert isinstance(cm.microcompact, Microcompact)
    assert cm.auto_compact is None


async def test_build_context_manager_session_memory_passthrough():
    """调用方（Session）先构造的 session_memory 应被原样注入 ContextManager。"""
    from agent.context import SessionMemory, SessionMemoryConfig

    sm = SessionMemory(
        SessionMemoryConfig(
            session_memory_dir=".agent/sessions",
            minimum_message_tokens_to_init=2000,
            minimum_tokens_between_update=2000,
            tool_calls_between_updates=3,
            enabled=True,
        ),
        session_id="sm-x",
    )
    cm = build_context_manager(Settings(), _model(), session_memory=sm)
    assert cm.session_memory is sm


async def test_build_system_prompt_includes_agents_md_fixed_base(monkeypatch, tmp_path):
    """AGENTS.md 固定底座须被读入 System Prompt 动态段（永不压缩）。"""
    (tmp_path / "AGENTS.md").write_text("# 项目约定：使用 pytest 跑测试", encoding="utf-8")
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    prompt = build_system_prompt(Settings(context={"agents_md_enabled": True}))
    assert "<system-reminder>" in prompt
    assert "AGENTS.md" in prompt
    assert "使用 pytest 跑测试" in prompt


async def test_build_system_prompt_excludes_agents_md_when_disabled(monkeypatch, tmp_path):
    """agents_md_enabled=False 时不读取/注入 AGENTS.md。"""
    (tmp_path / "AGENTS.md").write_text("# 不应出现", encoding="utf-8")
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    prompt = build_system_prompt(Settings(context={"agents_md_enabled": False}))
    assert "不应出现" not in prompt


async def test_build_system_prompt_static_prefix_stable_across_dates(monkeypatch):
    """静态段（system.md 渲染）稳定在前，日期变化不破坏前缀 → 利于 prompt cache。"""
    import agent.core.prompts as prompts

    class _Stub:
        def __init__(self, iso):
            self._iso = iso

        def today(self):
            return self

        def isoformat(self):
            return self._iso

    monkeypatch.setattr(prompts, "date", _Stub("2026-01-01"))
    p1 = build_system_prompt(Settings())
    monkeypatch.setattr(prompts, "date", _Stub("2026-12-31"))
    p2 = build_system_prompt(Settings())

    marker = "## 当前日期"
    assert p1.split(marker)[0] == p2.split(marker)[0]  # 静态前缀完全相同
    assert "2026-01-01" in p1 and "2026-12-31" in p2  # 动态段日期各自变化


async def test_build_system_prompt_includes_cwd(monkeypatch, tmp_path):
    """动态段须注入当前工作目录路径；设置 AGENT_PROJECT_ROOT 时也展示项目根。"""
    from pathlib import Path as _Path

    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    prompt = build_system_prompt(Settings())
    assert "## 当前工作目录" in prompt
    assert str(_Path.cwd()) in prompt
    assert "AGENT_PROJECT_ROOT" in prompt
    assert str(tmp_path) in prompt


async def test_read_agents_md_priority_project_root_over_user(monkeypatch, tmp_path):
    """读取优先级：项目根 AGENTS.md > 用户级 AGENTS.md。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("PROJECT-ROOT-CONTENT", encoding="utf-8")
    user = tmp_path / "user"
    user.mkdir()
    (user / "AGENTS.md").write_text("USER-CONTENT", encoding="utf-8")
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(proj))
    monkeypatch.setenv("AGENT_USER_CONFIG_DIR", str(user))
    assert _read_agents_md(Settings()) == "PROJECT-ROOT-CONTENT"


async def test_session_builds_context_mgr_when_any_enabled_else_none():
    """Session：启用任一压缩能力时构建 context_mgr；全关时保持 None（零开销）。"""
    from agent.core.session import Session

    sess_on = Session(_model(), default_registry, Settings(), tracer=None)
    assert sess_on.context_mgr is not None

    sess_off = Session(
        _model(),
        default_registry,
        Settings(
            context={
                "microcompact_enabled": False,
                "auto_compact_enabled": False,
                "session_memory_enabled": False,
            }
        ),
        tracer=None,
    )
    assert sess_off.context_mgr is None


async def test_loop_run_applies_microcompact_and_tracks_file_access():
    """M4.5：loop.run 经 context_mgr 应用 microcompact（None guard），并在工具执行后记录文件访问。"""
    reg = ToolRegistry()
    reg.register(tool("read", risk="read")(lambda args: ToolResult(ok=True, output="x" * 200)))

    model = FakeModel(
        [
            Decision(tool_calls=[ToolCall(id="c1", name="read", arguments={"path": "a.py"})]),
            Decision(text="done"),
        ]
    )
    loop = AgentLoop(model, reg, Settings())

    cm = build_context_manager(Settings(), _model())
    result = await loop.run("task", context_mgr=cm)

    assert result.text == "done"
    # _exec_tools 收到 context_mgr 并跟踪了 read 的文件访问（防漂移）
    assert "a.py" in cm.recent_files
