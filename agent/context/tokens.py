"""共享 token 估算工具（无 tiktoken 依赖）。

集中放置以避免在 ``agent.runtime.terminal_transport`` 与 ``agent.context`` 之间
重复实现，并规避潜在的循环导入：``_estimate_tokens`` 不依赖任何其它 agent 模块。
"""

from __future__ import annotations


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算：CJK 按 1 token/字，其余按 ~4 字符/token（无 tiktoken 依赖）。"""
    if not text:
        return 0
    cjk = sum(1 for c in text if ord(c) > 0x2E80)
    other = len(text) - cjk
    return cjk + other // 4 + 1
