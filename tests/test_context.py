"""M4.1 ContextManager 基础测试。

覆盖：计量分类 / 总量恒等式 / 阈值触发 / 边界排除 / 历史记录 / 配置可读。
"""

from __future__ import annotations

import pytest

from agent.config.settings import ContextConfig, Settings
from agent.context import (
    CompactRecord,
    Compactor,
    ContextManager,
    ContextUsage,
    Microcompact,
    PLACEHOLDER,
)
from agent.context.compactors import Compactor as CompactorAlias
from agent.core.model import Message, ToolCall


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
