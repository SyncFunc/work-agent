"""ApprovalGate（M2.2）单测：四模式矩阵、exec_policy、HITL、规则匹配。

覆盖：
- 四模式 × 三风险矩阵
- unless-trusted 模式 + exec_policy 免审
- on-request 仅 approval_request 时 ASK
- HITL 回调与非交互默认
- 规则匹配（前缀/正则/归一化）
- decide 纯函数性
"""

from __future__ import annotations

import pytest

from agent.runtime.approval import Action, ApprovalGate, ApprovalMode, Decision
from agent.runtime.sandbox import SandboxProfile


def _act(tool: str, risk: str, cmd: str | None = None, path: str | None = None, **kw) -> Action:
    if cmd is not None:
        args = {"cmd": cmd}
    elif path is not None:
        args = {"path": path}
    else:
        args = {}
    return Action(tool=tool, risk=risk, args=args, description=f"{tool}: {cmd or path}", **kw)


class _FakeTransport:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.last: Action | None = None

    @property
    def interactive(self) -> bool:
        return True

    async def approve(self, action: Action) -> bool:
        self.last = action
        return self.answer


# --------------------------------------------------------------------------- #
# 决策矩阵：read / edit / exec 在各模式下的 verdict
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "mode,risk,verdict",
    [
        (ApprovalMode.ON_REQUEST, "read", "allow"),
        (ApprovalMode.ON_REQUEST, "edit", "allow"),
        (ApprovalMode.ON_REQUEST, "exec", "allow"),
        (ApprovalMode.UNLESS_TRUSTED, "read", "allow"),
        (ApprovalMode.UNLESS_TRUSTED, "edit", "ask"),
        (ApprovalMode.UNLESS_TRUSTED, "exec", "ask"),
        (ApprovalMode.ON_FAILURE, "read", "allow"),
        (ApprovalMode.ON_FAILURE, "edit", "allow"),
        (ApprovalMode.ON_FAILURE, "exec", "allow"),
        (ApprovalMode.NEVER, "read", "allow"),
        (ApprovalMode.NEVER, "edit", "allow"),
        (ApprovalMode.NEVER, "exec", "allow"),
    ],
)
def test_mode_matrix(mode, risk, verdict):
    gate = ApprovalGate(mode)
    assert gate.decide(_act("tool", risk, cmd="x")).verdict == verdict


def test_unless_trused_read_allowed():
    gate = ApprovalGate(ApprovalMode.UNLESS_TRUSTED)
    assert gate.decide(_act("read", "read", path="a.txt")).verdict == "allow"


# --------------------------------------------------------------------------- #
# unless-trusted + exec_policy：命中者免审
# --------------------------------------------------------------------------- #
def test_exec_policy_skips_ask_in_unless_trused():
    """exec_policy 命中 → 短路 ALLOW（仅 unless-trusted 有效）。"""
    gate = ApprovalGate(ApprovalMode.UNLESS_TRUSTED, exec_policy=["ls "])
    d = gate.decide(_act("bash", "exec", cmd="ls -la"))
    assert d.verdict == "allow"


def test_exec_policy_no_effect_on_other_modes():
    """exec_policy 在 on-request/never 模式下不生效。"""
    for mode in (ApprovalMode.ON_REQUEST, ApprovalMode.NEVER, ApprovalMode.ON_FAILURE):
        gate = ApprovalGate(mode, exec_policy=["rm "])
        d = gate.decide(_act("bash", "exec", cmd="rm -rf x"))
        # on-request → ALLOW（默认放行），never → ALLOW，on-failure → ALLOW
        assert d.verdict == "allow"


# --------------------------------------------------------------------------- #
# on-request：模型显式 approval_request 才 ASK
# --------------------------------------------------------------------------- #
def test_on_request_asks_only_when_model_requests():
    gate = ApprovalGate(ApprovalMode.ON_REQUEST)
    assert gate.decide(_act("bash", "exec", cmd="rm x", approval_request=True)).verdict == "ask"
    assert gate.decide(_act("bash", "exec", cmd="rm x")).verdict == "allow"


# --------------------------------------------------------------------------- #
# authorize：HITL 与非交互默认
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_noninteractive_ask_defaults_to_allow():
    gate = ApprovalGate(ApprovalMode.UNLESS_TRUSTED)
    assert await gate.authorize(_act("bash", "exec", cmd="ls")) is True


@pytest.mark.asyncio
async def test_noninteractive_ask_defaults_to_deny_when_configured():
    gate = ApprovalGate(ApprovalMode.UNLESS_TRUSTED, noninteractive_default="deny")
    assert await gate.authorize(_act("bash", "exec", cmd="ls")) is False


@pytest.mark.asyncio
async def test_transport_false_rejects():
    gate = ApprovalGate(ApprovalMode.UNLESS_TRUSTED)
    transport = _FakeTransport(False)
    assert await gate.authorize(_act("bash", "exec", cmd="rm x"), transport) is False


@pytest.mark.asyncio
async def test_transport_true_allows():
    transport = _FakeTransport(True)
    gate = ApprovalGate(ApprovalMode.UNLESS_TRUSTED)
    action = _act("bash", "exec", cmd="rm x")
    assert await gate.authorize(action, transport) is True
    assert transport.last is action


# --------------------------------------------------------------------------- #
# 规则匹配：前缀 / 正则 / 归一化
# --------------------------------------------------------------------------- #
def test_exec_policy_prefix_match():
    gate = ApprovalGate(ApprovalMode.UNLESS_TRUSTED, exec_policy=["ls ", "cat "])
    assert gate.decide(_act("bash", "exec", cmd="ls -la")).verdict == "allow"
    assert gate.decide(_act("bash", "exec", cmd="cat x.txt")).verdict == "allow"
    assert gate.decide(_act("bash", "exec", cmd="rm x")).verdict == "ask"


def test_exec_policy_regex_match():
    gate = ApprovalGate(ApprovalMode.UNLESS_TRUSTED, exec_policy=[r"/^git (status|log|diff)/"])
    assert gate.decide(_act("bash", "exec", cmd="git status")).verdict == "allow"
    assert gate.decide(_act("bash", "exec", cmd="git push")).verdict == "ask"


def test_sudo_normalized_for_exec_policy():
    gate = ApprovalGate(ApprovalMode.UNLESS_TRUSTED, exec_policy=["ls "])
    assert gate.decide(_act("bash", "exec", cmd="sudo ls -la")).verdict == "allow"


def test_path_match_for_fs_tools():
    gate = ApprovalGate(ApprovalMode.UNLESS_TRUSTED, exec_policy=["src/"])
    assert gate.decide(_act("write", "edit", path="src/main.py")).verdict == "allow"
    assert gate.decide(_act("write", "edit", path="/etc/passwd")).verdict == "ask"


# --------------------------------------------------------------------------- #
# decide 纯函数性
# --------------------------------------------------------------------------- #
def test_decide_is_pure():
    gate = ApprovalGate(ApprovalMode.UNLESS_TRUSTED)
    action = _act("bash", "exec", cmd="rm x")
    d1 = gate.decide(action)
    d2 = gate.decide(action)
    assert d1 == d2
    assert d1.verdict == "ask"


def test_accepts_mode_string():
    gate = ApprovalGate("unless-trusted")
    assert gate.mode is ApprovalMode.UNLESS_TRUSTED
    assert gate.decide(_act("bash", "exec", cmd="ls")).verdict == "ask"


# --------------------------------------------------------------------------- #
# 提权自动计算
# --------------------------------------------------------------------------- #
def test_elevation_network_ask_target_danger_full():
    """联网命令 ASK → elevated_profile = danger-full。"""
    gate = ApprovalGate(ApprovalMode.UNLESS_TRUSTED)
    d = gate.decide(_act("bash", "exec", cmd="curl http://example.com"))
    assert d.verdict == "ask"
    assert d.elevated_profile == SandboxProfile.DANGER_FULL


def test_elevation_write_in_readonly_target_workspace_write():
    """read-only profile 下写命令 ASK → elevated_profile = workspace-write。"""
    gate = ApprovalGate(ApprovalMode.UNLESS_TRUSTED, sandbox_profile="read-only")
    d = gate.decide(_act("bash", "exec", cmd="touch x.txt"))
    assert d.verdict == "ask"
    assert d.elevated_profile == SandboxProfile.WORKSPACE_WRITE


def test_no_elevation_when_workspace_write_and_write():
    """workspace-write 下写命令 ASK → 不提权（已可写）。"""
    gate = ApprovalGate(ApprovalMode.UNLESS_TRUSTED, sandbox_profile="workspace-write")
    d = gate.decide(_act("bash", "exec", cmd="touch x.txt"))
    assert d.verdict == "ask"
    assert d.elevated_profile is None


def test_no_elevation_when_network_already_danger_full():
    """danger-full 下联网命令 ASK → 不提权。"""
    gate = ApprovalGate(ApprovalMode.UNLESS_TRUSTED, sandbox_profile="danger-full")
    d = gate.decide(_act("bash", "exec", cmd="curl http://example.com"))
    assert d.verdict == "ask"
    assert d.elevated_profile is None


def test_no_elevation_on_allow_verdict():
    """ALLOW 不提权（即使需要联网/写）。"""
    gate = ApprovalGate(ApprovalMode.NEVER, sandbox_profile="read-only")
    d = gate.decide(_act("bash", "exec", cmd="touch x.txt"))
    assert d.verdict == "allow"
    assert d.elevated_profile is None
