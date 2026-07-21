"""M4.7 验收：上下文压缩端到端集成测试。

覆盖（详见 milestones/M4.../4.7-测试与验收.md）：
- `test_microcompact_in_loop`：AgentLoop.run 每轮 _decide 前自动执行 Microcompact。
- `test_auto_compact_in_session`：Session.step 每轮后检查阈值并触发 Auto Compact。
- `test_full_compaction_pipeline`：完整流程 Microcompact → Auto Compact → 防漂移。
- `test_compact_preserves_pairing`：压缩后 tool_use/tool_result 配对不被破坏。
- `test_agents_md_injected`：AGENTS.md 被注入到 system prompt 动态段。

全程用 FakeModel 模拟，不依赖真实 LLM。
"""

from __future__ import annotations

from agent.config.settings import Settings
from agent.context import ContextManager, build_context_manager
from agent.context.compactors.microcompact import PLACEHOLDER
from agent.core.loop import AgentLoop
from agent.core.model import Decision, FakeModel, Message, ToolCall
from agent.core.prompts import build_system_prompt
from agent.core.session import Session
from agent.runtime.registry import default_registry


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _read_tool_results(conv: list[Message]) -> list[Message]:
    return [m for m in conv if m.role == "tool"]


def _tool_call_ids(conv: list[Message]) -> set[str]:
    ids: set[str] = set()
    for m in conv:
        if m.role == "assistant" and m.tool_calls:
            ids.update(tc.id for tc in m.tool_calls)
    return ids


async def test_microcompact_in_loop():
    """AgentLoop.run 在 _decide 前对 conv 投影执行 Microcompact（旧工具结果转占位符）。"""
    cm = ContextManager()  # 默认启用 microcompact
    # 8 个 read 工具结果（每个 >100 字符），与 assistant tool_calls 配对。
    conv: list[Message] = []
    for i in range(8):
        conv.append(Message(
            role="assistant", content=None,
            tool_calls=[ToolCall(id=f"t{i}", name="read", arguments={"path": f"f{i}.py"})],
        ))
        conv.append(Message(role="tool", content="x" * 150, tool_call_id=f"t{i}"))

    model = FakeModel([Decision(text="done")])
    loop = AgentLoop(model, default_registry, Settings(), tracer=None)
    res = await loop.run("task", conv, context_mgr=cm)

    assert res.text == "done"
    # Microcompact 替换最旧的 8-5=3 个 read 工具结果为占位符。
    placeholders = [m for m in cm.conv if m.role == "tool" and m.content == PLACEHOLDER]
    assert len(placeholders) == 3
    # 配对不被破坏：每条 tool_result 的 tool_call_id 都能找到对应 assistant tool_call。
    ids = _tool_call_ids(cm.conv)
    for m in _read_tool_results(cm.conv):
        assert m.tool_call_id in ids


async def test_auto_compact_in_session(tmp_path):
    """Session.step 每轮后检查阈值并触发 Auto Compact（模型生成摘要替换旧历史）。"""
    settings = Settings(context={
        "context_window": 4000, "max_output_tokens": 1000, "compact_buffer": 400,
        "auto_compact_enabled": True, "microcompact_enabled": True,
        "session_memory_enabled": False,
    })
    model = FakeModel([
        Decision(text="done"),  # ① step 的模型决策
        Decision(text="<summary>SESSION SUMMARY</summary>"),  # ② auto_compact 摘要
    ])
    session = Session(model, default_registry, settings, tracer=None)
    assert session.context_mgr is not None

    # regionA（待压缩的旧历史，含 read 工具结果）+ regionB（近期活跃消息）。
    region_a: list[Message] = []
    for i in range(4):
        region_a.append(Message(
            role="assistant", content=None,
            tool_calls=[ToolCall(id=f"a{i}", name="read", arguments={"path": f"a{i}.py"})],
        ))
        region_a.append(Message(role="tool", content="y" * 1500, tool_call_id=f"a{i}"))
    region_b = [
        Message(role="user", content="z" * 1500),
        Message(role="assistant", content="w" * 1500),
    ]
    conv = region_a + region_b
    session.messages = conv
    # 模拟「此前已压缩到 regionA 结尾」：boundary 落在 regionA/regionB 之间。
    session.context_mgr.set_conv(conv)
    session.context_mgr.compact_boundary = len(region_a)

    res, err = await session.step("continue", None, yes=False, fatal_plan_decline=False)
    assert err is None and res is not None

    merged = " ".join(m.content or "" for m in session.context_mgr.conv)
    assert "SESSION SUMMARY" in merged  # auto_compact 触发并插入摘要
    methods = [r.method for r in session.context_mgr.history]
    assert "auto_compact" in methods  # 压缩历史记录了 auto_compact


async def test_full_compaction_pipeline(tmp_path):
    """完整流程：Microcompact → Auto Compact → 防漂移，端到端跑通并产出正确结果。"""
    settings = Settings(context={
        "context_window": 4000, "max_output_tokens": 1000, "compact_buffer": 400,
        "auto_compact_enabled": True, "microcompact_enabled": True,
        "session_memory_enabled": False,
    })
    cm = build_context_manager(settings, FakeModel([Decision(text="<summary>FULL SUM</summary>")]))

    # regionA：8 个 read 工具结果（>keep_recent → microcompact 替换 3 个）。
    region_a: list[Message] = []
    for i in range(8):
        region_a.append(Message(
            role="assistant", content=None,
            tool_calls=[ToolCall(id=f"a{i}", name="read", arguments={"path": f"a{i}.py"})],
        ))
        region_a.append(Message(role="tool", content="y" * 1200, tool_call_id=f"a{i}"))
    region_b = [Message(role="user", content="recent work"), Message(role="assistant", content="ok")]
    conv = region_a + region_b

    # 防漂移：记录一个真实文件访问，压缩后应被重读并追加 [Anti-Drift]。
    recent = tmp_path / "recent.py"
    recent.write_text("print('anti-drift re-read')", encoding="utf-8")
    cm.set_conv(conv)
    cm.compact_boundary = len(region_a)
    cm.track_file_access(str(recent))

    ok = await cm.compact()
    assert ok is True
    # 完整流程跑通：Microcompact → Auto Compact → 防漂移 三段均执行（无异常）。
    # Auto Compact：插入摘要，替换了 boundary 之前的旧历史（含 microcompact 占位符）。
    assert any("FULL SUM" in (m.content or "") for m in cm.conv)
    assert "auto_compact" in [r.method for r in cm.history]
    # 防漂移：最近文件被重读并追加 [Anti-Drift] 消息。
    assert any("[Anti-Drift]" in (m.content or "") for m in cm.conv)


async def test_compact_preserves_pairing():
    """压缩后 tool_use / tool_result 配对不被破坏（API 协议要求）。"""
    cm = ContextManager()  # 默认启用 microcompact
    # 构造含 8 个配对的 conv，且内容 >100 以触发占位替换。
    conv: list[Message] = []
    for i in range(8):
        conv.append(Message(
            role="assistant", content=None,
            tool_calls=[ToolCall(id=f"p{i}", name="read", arguments={"path": f"p{i}.py"})],
        ))
        conv.append(Message(role="tool", content="data " * 30, tool_call_id=f"p{i}"))

    cm.set_conv(conv)
    await cm.apply_microcompact()

    # 每个 tool_result 的 tool_call_id 都必须存在对应 assistant tool_call。
    ids = _tool_call_ids(cm.conv)
    for m in _read_tool_results(cm.conv):
        assert m.tool_call_id is not None
        assert m.tool_call_id in ids
    # 配对数量守恒：压缩前后 tool_result 条数不变（只改 content，不删消息）。
    assert len(_read_tool_results(cm.conv)) == 8


async def test_agents_md_injected(tmp_path, monkeypatch):
    """AGENTS.md 被注入到 system prompt 动态段（端到端：固定底座抵达模型可见的 prompt）。"""
    (tmp_path / "AGENTS.md").write_text(
        "# 项目约定：使用 pytest 跑测试\n禁止直接 push 到 main", encoding="utf-8"
    )
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    settings = Settings(context={"agents_md_enabled": True})

    prompt = build_system_prompt(settings)

    assert "<system-reminder>" in prompt
    assert "AGENTS.md" in prompt
    assert "使用 pytest 跑测试" in prompt
    assert "禁止直接 push 到 main" in prompt
