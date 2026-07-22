"""M4.1 ContextManager 基础测试。

覆盖：计量分类 / 总量恒等式 / 阈值触发 / 边界排除 / 历史记录 / 配置可读。
"""

from __future__ import annotations

import pytest

from agent.config.settings import ContextConfig, Settings
from agent.context import (
    PLACEHOLDER,
    AutoCompact,
    Compactor,
    CompactRecord,
    ContextManager,
    ContextUsage,
    Microcompact,
    SessionMemory,
    SessionMemoryConfig,
)
from agent.context.compactors import Compactor as CompactorAlias
from agent.core.model import Decision, Message, ToolCall
from agent.obs.tracer import Tracer


# --------------------------------------------------------------------------- #
# 构造辅助
# --------------------------------------------------------------------------- #
def _msg(role: str, content: str = "", tool_calls=None, tool_call_id=None) -> Message:
    return Message(role=role, content=content, tool_calls=tool_calls, tool_call_id=tool_call_id)


def _big_cjk(n: int = 150_000) -> str:
    """生成 n 个 CJK 字符（1 字 = 1 token），用于逼近压缩阈值。"""
    return "中" * n


# --------------------------------------------------------------------------- #
# 构造与默认值
# --------------------------------------------------------------------------- #
def test_construction_defaults():
    m = ContextManager()
    assert m.context_window == 200_000
    assert m.max_output_tokens == 20_000
    assert m.compact_buffer == 13_000
    # effective_window = 200_000 - min(20_000, 20_000) = 180_000
    assert m.effective_window == 180_000
    # compact_threshold = 180_000 - 13_000 = 167_000
    assert m.compact_threshold == 167_000
    assert m.compact_boundary == 0
    assert m.history == []


def test_construction_effective_window_caps_output():
    # 当 max_output_tokens 超过 20_000 时，只保留 20_000 预算
    m = ContextManager(max_output_tokens=100_000)
    assert m.effective_window == 200_000 - 20_000


# --------------------------------------------------------------------------- #
# 计量
# --------------------------------------------------------------------------- #
def test_empty_usage_total_invariant():
    m = ContextManager()
    u = m.estimate_usage()
    assert isinstance(u, ContextUsage)
    # 空历史：messages = 0；total = fixed + dynamic + tools
    assert u.messages == 0
    assert u.total == u.system_fixed + u.system_dynamic + u.tools
    assert u.available == m.effective_window - u.total
    assert u.used_pct == pytest.approx(u.total / m.effective_window)


def test_usage_messages_counted_and_invariant():
    m = ContextManager()
    m.set_conv([_msg("user", _big_cjk(1_000)), _msg("assistant", "回复")])
    u = m.estimate_usage()
    # 总量恒等式恒成立
    assert u.total == u.system_fixed + u.system_dynamic + u.tools + u.messages
    assert u.messages > 0


def test_used_pct_within_range():
    m = ContextManager()
    u = m.estimate_usage()
    assert 0.0 <= u.used_pct <= 1.0


def test_estimate_counts_tool_calls_and_receipts():
    m = ContextManager()
    conv = [
        _msg("assistant", tool_calls=[ToolCall(id="c1", name="bash", arguments={"cmd": "ls"})]),
        _msg("tool", content="output text", tool_call_id="c1"),
    ]
    m.set_conv(conv)
    u = m.estimate_usage()
    # 至少包含 tool 回执内容与 tool_call 的 name+args
    assert u.messages > 0


# --------------------------------------------------------------------------- #
# 阈值触发
# --------------------------------------------------------------------------- #
def test_should_compact_false_when_empty():
    m = ContextManager()
    assert m.should_compact() is False


def test_should_compact_true_high_fixed_tokens():
    # 固定底座本身已逼近阈值 → 即便空历史也触发
    m = ContextManager(system_fixed_tokens=175_000)
    assert m.should_compact() is True


def test_should_compact_true_with_big_messages():
    m = ContextManager()
    m.set_conv([_msg("user", _big_cjk(150_000))])
    # 150_000 tokens + ~18_000 固定 > 167_000 阈值 → 触发
    assert m.should_compact() is True


# --------------------------------------------------------------------------- #
# 边界
# --------------------------------------------------------------------------- #
def test_mark_boundary_equals_conv_len():
    m = ContextManager()
    m.set_conv([_msg("user", "a"), _msg("assistant", "b"), _msg("user", "c")])
    m.mark_boundary()
    assert m.compact_boundary == len(m.conv) == 3


def test_boundary_excludes_old_from_estimate():
    m = ContextManager()
    old = [_msg("user", _big_cjk(50_000))]
    m.set_conv(old)
    before = m.estimate_usage().messages
    m.mark_boundary()
    # 边界后追加新消息，计量只统计新消息
    m.conv.append(_msg("assistant", "新"))
    after = m.estimate_usage().messages
    assert before > 0
    assert after < before


# --------------------------------------------------------------------------- #
# 历史记录
# --------------------------------------------------------------------------- #
def test_record_compact_appends_history():
    m = ContextManager()
    rec = m.record_compact("auto_compact", 100_000, 5_000)
    assert isinstance(rec, CompactRecord)
    assert len(m.history) == 1
    assert m.history[0].method == "auto_compact"
    assert m.history[0].before_tokens == 100_000
    assert m.history[0].after_tokens == 5_000
    assert rec.ts > 0


def test_record_compact_multiple():
    m = ContextManager()
    m.record_compact("microcompact", 10, 10)
    m.record_compact("auto_compact", 100, 20)
    assert len(m.history) == 2


# --------------------------------------------------------------------------- #
# 投影访问
# --------------------------------------------------------------------------- #
def test_get_active_messages_returns_copy():
    m = ContextManager()
    conv = [_msg("user", "x")]
    m.set_conv(conv)
    active = m.get_active_messages()
    assert active == conv
    active.append(_msg("assistant", "y"))
    # 返回的是副本，不应影响内部 conv
    assert len(m.conv) == 1


def test_set_conv_updates_internal():
    m = ContextManager()
    conv = [_msg("user", "hello")]
    m.set_conv(conv)
    assert m.conv is conv


# --------------------------------------------------------------------------- #
# 配置可读
# --------------------------------------------------------------------------- #
def test_settings_context_default():
    settings = Settings()
    assert isinstance(settings.context, ContextConfig)
    assert settings.context.context_window == 200_000
    assert settings.context.max_output_tokens == 20_000
    assert settings.context.compact_buffer == 13_000
    assert settings.context.microcompact_keep_recent == 5
    assert settings.context.microcompact_enabled is True
    assert settings.context.auto_compact_enabled is True
    assert settings.context.session_memory_enabled is True
    assert settings.context.session_memory_dir == ".agent/sessions"
    assert settings.context.agents_md_path == "AGENTS.md"
    assert settings.context.agents_md_enabled is True


# --------------------------------------------------------------------------- #
# 导出与协议
# --------------------------------------------------------------------------- #
def test_compactor_protocol_runtime_checkable():
    # 验证 Compactor 是 runtime_checkable Protocol，且可被 isinstance 检查
    assert Compactor is CompactorAlias

    class Dummy:
        async def compact(self, conv, boundary):
            return conv

    assert isinstance(Dummy(), Compactor)


# --------------------------------------------------------------------------- #
# M4.2 Microcompact
# --------------------------------------------------------------------------- #
def _tool_pair(name: str, content: str, i: int) -> list[Message]:
    """构造一对 assistant(tool_call) + tool(result)，tool_call_id 配对。"""
    return [
        _msg("assistant", tool_calls=[ToolCall(id=f"c{i}", name=name, arguments={"x": i})]),
        _msg("tool", content=content, tool_call_id=f"c{i}"),
    ]


def _many_bash_tool_pairs(n: int, content_len: int = 150) -> list[Message]:
    conv: list[Message] = []
    for i in range(n):
        conv.extend(_tool_pair("bash", "x" * content_len, i))
    return conv


async def test_microcompact_boundary_zero_noop():
    conv = _many_bash_tool_pairs(3)
    mc = Microcompact()
    out = await mc.compact(conv, 0)
    assert out is conv
    # boundary=0 不替换任何消息
    assert all(m.content != PLACEHOLDER for m in conv if m.role == "tool")


async def test_microcompact_replaces_old_keeps_recent_five():
    # 8 对 bash 工具结果（每对 content>100，来自可压缩工具）
    conv = _many_bash_tool_pairs(8)
    mc = Microcompact(keep_recent=5)
    await mc.compact(conv, len(conv))
    tool_msgs = [m for m in conv if m.role == "tool"]
    # 共 8 个 tool 结果：前 3 个被替换，后 5 个保留
    assert sum(1 for m in tool_msgs if m.content == PLACEHOLDER) == 3
    assert sum(1 for m in tool_msgs if m.content != PLACEHOLDER) == 5


async def test_microcompact_keep_recent_zero_replaces_all():
    conv = _many_bash_tool_pairs(6)
    mc = Microcompact(keep_recent=0)
    await mc.compact(conv, len(conv))
    tool_msgs = [m for m in conv if m.role == "tool"]
    # keep_recent=0 必须替换全部旧 tool 消息（边界修正：不能因 :-0 变空）
    assert all(m.content == PLACEHOLDER for m in tool_msgs)


async def test_microcompact_preserves_tool_call_id_and_role():
    conv = _many_bash_tool_pairs(4)
    mc = Microcompact(keep_recent=1)
    await mc.compact(conv, len(conv))
    for m in conv:
        if m.content == PLACEHOLDER:
            assert m.role == "tool"
            assert m.tool_call_id is not None
            assert m.tool_call_id.startswith("c")


async def test_microcompact_short_content_untouched():
    # 短 tool 结果（≤100 字符）不应被替换
    conv = _tool_pair("bash", "short", 0)
    mc = Microcompact(keep_recent=0)
    await mc.compact(conv, len(conv))
    assert conv[1].content == "short"
    assert conv[1].content != PLACEHOLDER


async def test_microcompact_non_tool_messages_untouched():
    conv = [
        _msg("user", "用户指令很长" * 30),
        _msg("assistant", "助手回复很长" * 30),
        *_tool_pair("bash", "x" * 150, 0),
    ]
    mc = Microcompact(keep_recent=0)
    await mc.compact(conv, len(conv))
    # 非 tool 消息原样保留
    assert conv[0].content == "用户指令很长" * 30
    assert conv[1].content == "助手回复很长" * 30
    # tool 消息被替换
    assert conv[2].content != PLACEHOLDER  # assistant 消息不变
    assert conv[3].content == PLACEHOLDER  # tool 结果被替换


async def test_microcompact_non_compactable_tool_untouched():
    # AskUserQuestion 等非大输出类工具结果即便很长也不压缩
    conv = _tool_pair("AskUserQuestion", "x" * 200, 0)
    mc = Microcompact(keep_recent=0)
    await mc.compact(conv, len(conv))
    assert conv[1].content == "x" * 200
    assert conv[1].content != PLACEHOLDER


async def test_microcompact_boundary_limits_scope():
    # 构造 10 对，boundary=10 → 只处理前 10 条消息（前 5 对）的 tool 结果
    conv = _many_bash_tool_pairs(10)
    mc = Microcompact(keep_recent=0)
    await mc.compact(conv, 10)  # 只把前 10 条消息（前 5 对）作为压缩区
    tool_msgs = [m for m in conv if m.role == "tool"]
    # 前 5 对（索引 0..9）的 tool 结果全部替换，后 5 对保留
    assert all(m.content == PLACEHOLDER for m in tool_msgs[:5])
    assert all(m.content != PLACEHOLDER for m in tool_msgs[5:])


async def test_microcompact_is_compactor():
    assert isinstance(Microcompact(), Compactor)


async def test_context_manager_apply_microcompact():
    m = ContextManager()
    conv = _many_bash_tool_pairs(8)
    m.set_conv(conv)
    out = await m.apply_microcompact()
    # 原地替换，返回的是同一个列表
    assert out is m.conv
    tool_msgs = [x for x in m.conv if x.role == "tool"]
    # 默认 keep_recent=5 → 前 3 个替换，后 5 个保留
    assert sum(1 for x in tool_msgs if x.content == PLACEHOLDER) == 3


async def test_context_manager_apply_microcompact_empty():
    m = ContextManager()
    out = await m.apply_microcompact()
    assert out == []


# --------------------------------------------------------------------------- #
# M4.3 AutoCompact
# --------------------------------------------------------------------------- #
def _fake_model(script: list[Decision]) -> object:
    """返回一个具有 act 方法的假模型。"""

    class _Fake:
        def __init__(self, s: list[Decision]):
            self._script = list(s)

        async def act(self, messages, tools=None):
            if not self._script:
                return Decision(text="<script exhausted>")
            return self._script.pop(0)

    return _Fake(script)


async def test_auto_compact_boundary_zero_noop():
    model = _fake_model([Decision(text="<summary>test</summary>")])
    ac = AutoCompact(model)
    out = await ac.compact([], 0)
    assert out == []


async def test_auto_compact_summary_tag_parsed():
    model = _fake_model([Decision(text="<summary>压缩后的摘要内容</summary>")])
    ac = AutoCompact(model)
    conv = [
        Message(role="user", content="第一段长历史" * 200),
        Message(role="assistant", content="回复" * 50),
    ]
    out = await ac.compact(conv, len(conv))
    assert len(out) == 1  # 摘要替换了全部历史
    assert "[Compact Summary]" in (out[0].content or "")
    assert "压缩后的摘要内容" in (out[0].content or "")


async def test_auto_compact_no_tag_fallback():
    model = _fake_model([Decision(text="纯文本摘要，没有标签")])
    ac = AutoCompact(model)
    conv = [Message(role="user", content="历史")]
    out = await ac.compact(conv, len(conv))
    assert len(out) == 1
    assert "纯文本摘要" in (out[0].content or "")


async def test_auto_compact_preserves_active_messages():
    model = _fake_model([Decision(text="<summary>摘要</summary>")])
    ac = AutoCompact(model)
    conv = [
        Message(role="user", content="旧历史"),
        Message(role="user", content="新活跃消息"),
    ]
    out = await ac.compact(conv, 1)  # boundary=1：只压缩第 1 条
    assert len(out) == 2  # 摘要 + 第 2 条
    assert "[Compact Summary]" in (out[0].content or "")
    assert out[1].content == "新活跃消息"


async def test_auto_compact_failure_breaker():
    # 连续返回 None → 3 次后放弃
    class _AlwaysFail:
        async def act(self, messages, tools=None):
            raise ValueError("模型挂了")

    ac = AutoCompact(_AlwaysFail(), max_failures=3)
    conv = [Message(role="user", content="历史")]
    # 第一次
    out1 = await ac.compact(conv, len(conv))
    assert out1 is conv  # 原样返回
    assert ac.failure_count == 1
    # 第二次
    out2 = await ac.compact(conv, len(conv))
    assert out2 is conv
    assert ac.failure_count == 2
    # 第三次
    out3 = await ac.compact(conv, len(conv))
    assert out3 is conv
    assert ac.failure_count == 3
    # 第四次（已超 max_failures，直接跳过不增加）
    out4 = await ac.compact(conv, len(conv))
    assert out4 is conv
    assert ac.failure_count == 3  # 不增加，直接跳过


async def test_auto_compact_success_resets_failure_count():
    model = _fake_model([Decision(text="<summary>ok</summary>")])
    ac = AutoCompact(model, max_failures=3)
    ac.failure_count = 2
    conv = [Message(role="user", content="历史")]
    out = await ac.compact(conv, len(conv))
    assert out is not conv  # 成功压缩
    assert ac.failure_count == 0  # 重置


async def test_auto_compact_is_compactor():
    model = _fake_model([])
    assert isinstance(AutoCompact(model), Compactor)


async def test_auto_compact_empty_history():
    model = _fake_model([Decision(text="<summary>摘要</summary>")])
    ac = AutoCompact(model)
    out = await ac.compact([], len([]))
    assert out == []


async def test_auto_compact_format_history_includes_tool_calls():
    model = _fake_model([Decision(text="<summary>摘要</summary>")])
    ac = AutoCompact(model)
    conv = [
        Message(
            role="assistant", tool_calls=[ToolCall(id="t1", name="bash", arguments={"cmd": "ls"})]
        ),
        Message(role="tool", content="file1\nfile2", tool_call_id="t1"),
    ]
    out = await ac.compact(conv, len(conv))
    assert len(out) == 1
    assert "[Compact Summary]" in (out[0].content or "")


# --------------------------------------------------------------------------- #
# M4.3 ContextManager compact() 集成
# --------------------------------------------------------------------------- #
async def test_context_manager_compact_with_auto_compact():
    model = _fake_model([Decision(text="<summary>摘要</summary>")])
    ac = AutoCompact(model)
    m = ContextManager(auto_compact=ac)
    conv = [
        Message(role="user", content="旧历史（将被压缩）" * 1000),
        Message(role="user", content="大段消息" * 150_000),  # boundary 后的大消息触发阈值
    ]
    m.set_conv(conv)
    # 标记边界：压缩第 1 条，保留第 2 条活跃消息
    m.compact_boundary = 1
    result = await m.compact()
    assert result is True
    # 压缩后 conv 发生了变化（摘要替换旧历史）
    assert len(m.history) == 1
    assert m.history[0].method == "auto_compact"
    # 摘要替换了边界前的历史
    assert "[Compact Summary]" in (m.conv[0].content or "")
    # 活跃消息仍保留（anti_drift 追加了，所以可能是第 3 条）
    assert any("大段消息" in (x.content or "") for x in m.conv)


async def test_context_manager_compact_microcompact_only():
    # 不设 auto_compact，只执行 microcompact
    m = ContextManager()
    conv = _many_bash_tool_pairs(8)
    m.set_conv(conv)
    result = await m.compact()
    assert result is True
    # 只执行了 microcompact，没有 auto_compact
    tool_msgs = [x for x in m.conv if x.role == "tool"]
    assert sum(1 for x in tool_msgs if x.content == PLACEHOLDER) == 3


async def test_context_manager_track_file_access():
    m = ContextManager()
    m.track_file_access("a.py")
    m.track_file_access("b.py")
    m.track_file_access("a.py")
    assert m.recent_files == ["a.py", "b.py", "a.py"]


async def test_context_manager_track_file_access_max_10():
    m = ContextManager()
    for i in range(15):
        m.track_file_access(f"f{i}.py")
    assert len(m.recent_files) == 10
    assert m.recent_files[-1] == "f14.py"


async def test_anti_drift_appends_message(tmp_path):
    m = ContextManager()
    f1 = tmp_path / "test.txt"
    f1.write_text("hello world")
    m.track_file_access(str(f1))
    m.set_conv([Message(role="user", content="hi")])
    await m._anti_drift()
    assert len(m.conv) == 2
    assert "[Anti-Drift]" in (m.conv[-1].content or "")
    assert "test.txt" in (m.conv[-1].content or "")


# --------------------------------------------------------------------------- #
# M4.3 同期：压缩流程的 trace 埋点
# --------------------------------------------------------------------------- #
async def test_compact_emits_trace_tree():
    """完整压缩流程应记录 context.compact / microcompact / model.act 三层 span，
    model.act 与 microcompact 通过 contextvars 自动成为 context.compact 的子 span。"""
    tracer = Tracer()
    model = _fake_model([Decision(text="<summary>摘要</summary>")])
    ac = AutoCompact(model, tracer=tracer)
    m = ContextManager(auto_compact=ac, tracer=tracer)
    conv = [
        Message(role="user", content="旧历史（将被压缩）" * 1000),
        Message(role="user", content="大段消息" * 150_000),  # 触发阈值
    ]
    m.set_conv(conv)
    m.compact_boundary = 1
    await m.compact()

    names = {s.name for s in tracer.spans}
    assert "context.compact" in names
    assert "compact.microcompact" in names
    assert "compact.auto_compact" in names
    assert "model.act" in names

    ctx = next(s for s in tracer.spans if s.name == "context.compact")
    model_span = next(s for s in tracer.spans if s.name == "model.act")
    mc_span = next(s for s in tracer.spans if s.name == "compact.microcompact")
    ac_span = next(s for s in tracer.spans if s.name == "compact.auto_compact")
    # 隐式 parent 传递链：context.compact → compact.auto_compact → model.act
    # （microcompact 与 auto_compact 平级，都是 context.compact 的子 span）
    assert ac_span.parent_id == ctx.id
    assert model_span.parent_id == ac_span.id
    assert mc_span.parent_id == ctx.id


async def test_compact_trace_shortcut_no_auto_span():
    """microcompact 后未超阈值 → 跳过 auto_compact，不应产生 auto_compact / model.act span。"""
    tracer = Tracer()
    model = _fake_model([Decision(text="<summary>摘要</summary>")])
    ac = AutoCompact(model, tracer=tracer)
    m = ContextManager(auto_compact=ac, tracer=tracer)
    # 只有少量 tool 结果，microcompact 后不可能超阈值
    conv = _many_bash_tool_pairs(8)
    m.set_conv(conv)
    await m.compact()

    names = {s.name for s in tracer.spans}
    assert "context.compact" in names
    assert "compact.microcompact" in names
    assert "compact.auto_compact" not in names  # 捷径跳过，auto_compact 步骤根本未调用
    assert "model.act" not in names


async def test_auto_compact_model_act_span_records_usage():
    """model.act span 应把 Decision.usage 写入 meta（供导出 Langfuse 等）。"""
    tracer = Tracer()
    model = _fake_model(
        [
            Decision(
                text="<summary>摘要</summary>",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )
        ]
    )
    ac = AutoCompact(model, tracer=tracer)
    # 在 context.compact 隐式父 span 内调用，验证自动继承
    with tracer.span("context.compact", kind="compact"):
        out = await ac.compact(
            [Message(role="user", content="历史"), Message(role="user", content="活跃")], 1
        )
    assert len(out) == 2
    ac_span = next(s for s in tracer.spans if s.name == "compact.auto_compact")
    model_span = next(s for s in tracer.spans if s.name == "model.act")
    assert model_span.meta.get("usage", {}).get("total_tokens") == 15
    # parent 自动继承链：model.act → compact.auto_compact → context.compact
    ctx = next(s for s in tracer.spans if s.name == "context.compact")
    assert model_span.parent_id == ac_span.id
    assert ac_span.parent_id == ctx.id


async def test_compact_trace_noop_when_tracer_none():
    """未注入 tracer 时降级为 no-op，不报错且行为不变（保持现有用例契约）。"""
    model = _fake_model([Decision(text="<summary>摘要</summary>")])
    ac = AutoCompact(model)  # tracer=None
    m = ContextManager(auto_compact=ac)  # tracer=None
    conv = [
        Message(role="user", content="旧历史" * 1000),
        Message(role="user", content="大段消息" * 150_000),
    ]
    m.set_conv(conv)
    m.compact_boundary = 1
    result = await m.compact()
    assert result is True
    assert len(m.history) == 1


async def test_auto_compact_first_compaction_boundary_zero():
    """M4.6/修复：compact_boundary=0（首次压缩）不再 no-op，真正压缩更早历史。

    此前 boundary<=0 直接返回原 conv，导致第一次超阈值永不生成摘要；修复后自动保留
    最近 ``recent_keep`` 条消息、压缩更早部分。
    """
    model = _fake_model([Decision(text="<summary>最早的摘要</summary>")])
    ac = AutoCompact(model)
    m = ContextManager(auto_compact=ac)
    # 10 条大消息，远超阈值（compact_boundary 默认 0）
    conv = [_msg("user", _big_cjk(20_000)) for _ in range(10)]
    m.set_conv(conv)
    assert m.compact_boundary == 0
    ok = await m.compact()
    assert ok is True
    # 产生了 auto_compact 记录
    assert any(r.method == "auto_compact" for r in m.history)
    # 压缩后 conv 明显变短（10 条 → 1 摘要 + 最近 recent_keep 条）
    assert len(m.conv) < len(conv)
    # 最近消息被保留（摘要之后仍有原文）
    assert any("最早的摘要" in (x.content or "") for x in m.conv)


# --------------------------------------------------------------------------- #
# M4.4 Session Memory Compact


# --------------------------------------------------------------------------- #
def _sm_config(**kw) -> SessionMemoryConfig:
    return SessionMemoryConfig(**kw)


def test_session_memory_load_none(tmp_path):
    sm = SessionMemory(_sm_config(session_memory_dir=str(tmp_path)), session_id="s1")
    assert sm.load() is None


def test_session_memory_save_load_roundtrip_and_perms(tmp_path):
    import os
    import stat

    sm = SessionMemory(_sm_config(session_memory_dir=str(tmp_path)), session_id="s2")
    sm.save("我的摘要")
    assert sm.load() == "我的摘要"
    # 校验落盘位置与权限
    assert sm._summary_path.is_file()
    if os.name != "nt":  # Windows 不保证 chmod 语义
        fmode = stat.S_IMODE(sm._summary_path.stat().st_mode)
        dmode = stat.S_IMODE(sm._summary_path.parent.stat().st_mode)
        assert oct(fmode) == oct(0o600), f"文件权限应为 0o600，实际 {oct(fmode)}"
        assert oct(dmode) == oct(0o700), f"目录权限应为 0o700，实际 {oct(dmode)}"
    # meta sidecar 记录版本演进
    meta = sm.load_stats()
    assert meta.get("version") == 1


def test_session_memory_should_update_disabled(tmp_path):
    sm = SessionMemory(_sm_config(enabled=False, session_memory_dir=str(tmp_path)), session_id="s3")
    sm.save("摘要")
    assert sm.should_update(50_000, 10_000, 5, False) is False


def test_session_memory_should_update_init_threshold(tmp_path):
    sm = SessionMemory(_sm_config(session_memory_dir=str(tmp_path)), session_id="s4")
    # 未保存且无摘要，且上下文未达 init 阈值 → False
    assert sm.should_update(5_000, 10_000, 5, False) is False
    # 未保存但上下文已达 init 阈值 → 允许首次触发
    assert sm.should_update(15_000, 10_000, 5, False) is True


def test_session_memory_should_update_between_threshold(tmp_path):
    sm = SessionMemory(_sm_config(session_memory_dir=str(tmp_path)), session_id="s5")
    sm.save("已有摘要")
    # token 增量不足 → False
    assert sm.should_update(20_000, 1_000, 5, False) is False
    # token 增量达标且 tool call ≥ 3 → True
    assert sm.should_update(20_000, 6_000, 3, True) is True
    # token 增量达标、tool call 不足，但最后一轮无 tool（自然断点）→ True
    assert sm.should_update(20_000, 6_000, 0, False) is True
    # token 增量达标、tool call 不足，且最后一轮有 tool → False
    assert sm.should_update(20_000, 6_000, 0, True) is False


def test_session_memory_compact_with_summary(tmp_path):
    sm = SessionMemory(_sm_config(session_memory_dir=str(tmp_path)), session_id="s6")
    sm.save("## Session Title\n项目背景摘要")
    conv = [
        Message(role="user", content="旧历史1"),
        Message(role="user", content="旧历史2"),
        Message(role="user", content="活跃消息"),
    ]
    out = sm.compact(conv, boundary=2)
    assert out is not None
    assert len(out) == 2  # 摘要 + 1 条保留的活跃消息
    assert out[0].role == "user"
    assert "[Session Summary]" in (out[0].content or "")
    assert "项目背景摘要" in (out[0].content or "")
    assert out[1].content == "活跃消息"


def test_session_memory_compact_no_summary_returns_none(tmp_path):
    sm = SessionMemory(_sm_config(session_memory_dir=str(tmp_path)), session_id="s7")
    conv = [Message(role="user", content="x")]
    assert sm.compact(conv, boundary=0) is None


def test_session_memory_compact_recent_retention(tmp_path):
    """保留原文满足：≥ min_recent_messages(5) 且 ≤ max_recent_tokens(40000)（估算 ±10%）。"""
    sm = SessionMemory(_sm_config(session_memory_dir=str(tmp_path)), session_id="s8")
    sm.save("摘要")
    # boundary=0，recent = conv 全部；每条约 5000 tokens（CJK 1 token/字）
    conv = [Message(role="user", content="中" * 5_000) for _ in range(10)]
    out = sm.compact(conv, boundary=0)
    assert out is not None
    recent = out[1:]  # 去掉首条 [Session Summary]
    # 超过 max_recent_tokens(40000) 的部分被丢弃；但至少保留 5 条（min_recent_messages）
    assert len(recent) >= 5
    assert len(recent) <= 10
    # 总保留 token 不超过 max_recent_tokens 太多（±10% 容差）
    kept_tokens = sum(len(m.content or "") // 4 + 1 for m in recent)
    assert kept_tokens <= 40_000 * 1.1


async def test_context_manager_compact_prefers_session_memory(tmp_path):
    """有摘要时 ContextManager.compact() 优先走 Session Memory 分支，不触发 Auto Compact。"""
    sm = SessionMemory(_sm_config(session_memory_dir=str(tmp_path)), session_id="s9")
    sm.save("已有会话摘要")

    # Auto Compact 模型若被调用则抛错（验证确实未走到兜底）
    class _MustNotCall:
        async def act(self, messages, tools=None):
            raise AssertionError("Auto Compact 不应被调用（Session Memory 优先）")

    ac = AutoCompact(_MustNotCall())
    m = ContextManager(auto_compact=ac, session_memory=sm)
    conv = [
        Message(role="user", content="旧历史占位"),  # boundary 前，被摘要替换
        Message(role="user", content="大段活跃" * 150_000),  # post-boundary，触发阈值
        Message(role="user", content="活跃消息A"),
        Message(role="user", content="活跃消息B"),
    ]
    m.set_conv(conv)
    m.compact_boundary = 1
    result = await m.compact()
    assert result is True
    # 压缩方法记录应为 session_memory
    assert m.history[-1].method == "session_memory"
    assert "[Session Summary]" in (m.conv[0].content or "")
    # 活跃消息（boundary 后、且在保留额度内）仍保留
    assert any("活跃消息" in (x.content or "") for x in m.conv)


async def test_context_manager_compact_session_memory_fallback_to_auto(tmp_path):
    """无摘要时 Session Memory 返回 None，compact() 降级走 Auto Compact。"""
    sm = SessionMemory(_sm_config(session_memory_dir=str(tmp_path)), session_id="s10")
    # 注意：未 save → load() 为 None
    model = _fake_model([Decision(text="<summary>自动摘要</summary>")])
    ac = AutoCompact(model)
    m = ContextManager(auto_compact=ac, session_memory=sm)
    conv = [
        Message(role="user", content="旧历史" * 1000),
        Message(role="user", content="大段消息" * 150_000),
    ]
    m.set_conv(conv)
    m.compact_boundary = 1
    await m.compact()
    assert m.history[-1].method == "auto_compact"
    assert "[Compact Summary]" in (m.conv[0].content or "")


def test_session_memory_registered_as_builtin_agent():
    """记忆子 agent 作为内置类型被发现，且强隔离（无工具、无控制工具）。"""
    from agent.subagent import SubagentSpawner

    sp = SubagentSpawner(Settings())
    specs = {s.name: s for s in sp.discover()}
    assert "session-memory" in specs
    spec = specs["session-memory"]
    assert spec.builtin is True
    assert spec.tools == []  # 不暴露任何工具
    assert spec.no_control_tools is True  # 不注入控制/虚拟工具
    assert spec.share_history is True  # fork 父对话以读历史


async def test_session_memory_background_update_reuses_subagent(monkeypatch, tmp_path):
    """M4.4 复用 M5.4.1 后台 Subagent：触发后记忆子 agent 在后台跑，结果落盘 summary.md。"""
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    from agent.core.model import Decision, FakeModel
    from agent.core.session import Session
    from agent.runtime.registry import default_registry
    from agent.runtime.terminal_transport import TerminalTransport

    settings = Settings(
        subagents={"enabled": True},
        context={
            "session_memory_enabled": True,
            "auto_compact_enabled": True,
            "session_memory_dir": str(tmp_path),
        },
    )
    # 记忆子 agent 用同一个 FakeModel 产出 10 段摘要文本
    model = FakeModel([Decision(text="## Session Title\n后台生成的会话摘要")])
    sess = Session(model, default_registry, settings, tracer=None)
    assert sess.session_memory is not None

    transport = TerminalTransport(interactive=False)
    task_id = sess._trigger_session_memory_update(transport)
    assert task_id is not None
    assert task_id in sess._bg_tasks

    # 等待后台记忆子 agent 完成（复用 spawn_background 的 asyncio.Task）
    await sess._bg_tasks[task_id]
    assert task_id not in sess._bg_tasks  # 完成后从任务表移除
    # 结果由 result_sink 落盘到 summary.md（而非注入主对话）
    assert sess.session_memory.load() == "## Session Title\n后台生成的会话摘要"
    assert sess._sm_updating is False  # on_done 已释放串行化锁
    # 主对话未被污染（记忆结果不应作为 user 消息注入）
    assert all("[Background Subagent" not in (m.content or "") for m in sess.messages)
