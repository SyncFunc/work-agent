"""ApprovalGate（M2.2）单测：四模式矩阵、deny 优先、allow 短路、HITL、规则匹配。"""

from __future__ import annotations

import pytest

from agent.runtime.approval import Action, ApprovalGate, ApprovalMode, ApprovalUI, Decision


def _act(tool: str, risk: str, cmd: str | None = None, path: str | None = None, **kw) -> Action:
    if cmd is not None:
        args = {"cmd": cmd}
    elif path is not None:
        args = {"path": path}
    else:
        args = {}
    return Action(tool=tool, risk=risk, args=args, description=f"{tool}: {cmd or path}", **kw)


class _FakeUI:
    """可脚本化的 HITL 回调。"""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.last: Action | None = None

    async def approve(self, action: Action) -> bool:
        self.last = action
        return self.answer


# --------------------------------------------------------------------------- #
# 决策矩阵（§4.3）：read / edit / exec 在各模式下的 verdict
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "mode,risk,verdict",
    [
        (ApprovalMode.UNTRUSTED, "read", "allow"),
        (ApprovalMode.UNTRUSTED, "edit", "ask"),
        (ApprovalMode.UNTRUSTED, "exec", "ask"),
        (ApprovalMode.ON_REQUEST, "read", "allow"),
        (ApprovalMode.ON_REQUEST, "edit", "allow"),
        (ApprovalMode.ON_REQUEST, "exec", "allow"),
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


def test_untrusted_read_allowed_even_with_escalation_off():
    gate = ApprovalGate(ApprovalMode.UNTRUSTED)
    # 普通 read 在 untrusted 仍 ALLOW（只读不危险）
    assert gate.decide(_act("read", "read", path="a.txt")).verdict == "allow"


# --------------------------------------------------------------------------- #
# deny 优先（安全不变量）
# --------------------------------------------------------------------------- #
def test_deny_beats_never_mode():
    gate = ApprovalGate(ApprovalMode.NEVER, deny=["rm "])
    d = gate.decide(_act("bash", "exec", cmd="rm -rf x"))
    assert d.verdict == "deny"


def test_deny_beats_allow_shortcut():
    # 即便命中 allow，deny 仍优先（deny 在第 1 步）
    gate = ApprovalGate(ApprovalMode.UNTRUSTED, allow=["rm "], deny=["rm "])
    d = gate.decide(_act("bash", "exec", cmd="rm -rf x"))
    assert d.verdict == "deny"


def test_allow_shortcuts_ask_in_untrusted():
    # allow 命中 → 短路 ALLOW，跳过 untrusted 的 ASK
    gate = ApprovalGate(ApprovalMode.UNTRUSTED, allow=["git push"])
    d = gate.decide(_act("bash", "exec", cmd="git push origin main"))
    assert d.verdict == "allow"


# --------------------------------------------------------------------------- #
# escalated 提权强制 ASK（无视模式）
# --------------------------------------------------------------------------- #
def test_escalated_forces_ask_even_in_never():
    gate = ApprovalGate(ApprovalMode.NEVER)
    d = gate.decide(_act("bash", "exec", cmd="npm install x", escalated=True))
    assert d.verdict == "ask"


def test_escalated_forces_ask_even_in_on_request():
    gate = ApprovalGate(ApprovalMode.ON_REQUEST)
    d = gate.decide(_act("bash", "exec", cmd="npm install x", escalated=True, approval_request=False))
    assert d.verdict == "ask"


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
    gate = ApprovalGate(ApprovalMode.UNTRUSTED)  # 无 ui
    assert await gate.authorize(_act("bash", "exec", cmd="ls")) is True


@pytest.mark.asyncio
async def test_noninteractive_ask_defaults_to_deny_when_configured():
    gate = ApprovalGate(ApprovalMode.UNTRUSTED, noninteractive_default="deny")
    assert await gate.authorize(_act("bash", "exec", cmd="ls")) is False


@pytest.mark.asyncio
async def test_ui_false_rejects():
    gate = ApprovalGate(ApprovalMode.UNTRUSTED, ui=_FakeUI(False))
    assert await gate.authorize(_act("bash", "exec", cmd="rm x")) is False


@pytest.mark.asyncio
async def test_ui_true_allows():
    ui = _FakeUI(True)
    gate = ApprovalGate(ApprovalMode.UNTRUSTED, ui=ui)
    action = _act("bash", "exec", cmd="rm x")
    assert await gate.authorize(action) is True
    assert ui.last is action  # 回调收到同一个 Action


@pytest.mark.asyncio
async def test_authorize_deny_is_false_without_ui():
    # deny 直接 False，不进 HITL，也不受 noninteractive_default 影响
    gate = ApprovalGate(ApprovalMode.NEVER, deny=["rm "], noninteractive_default="allow")
    assert await gate.authorize(_act("bash", "exec", cmd="rm x")) is False


# --------------------------------------------------------------------------- #
# 规则匹配：前缀 / 正则 / 归一化
# --------------------------------------------------------------------------- #
def test_prefix_rule_matches():
    gate = ApprovalGate(ApprovalMode.NEVER, deny=["rm "])
    assert gate.decide(_act("bash", "exec", cmd="rm -rf build/")).verdict == "deny"


def test_regex_rule_matches():
    gate = ApprovalGate(ApprovalMode.NEVER, deny=[r"/^curl .*example\.com/"])
    assert gate.decide(_act("bash", "exec", cmd="curl https://example.com/x.sh")).verdict == "deny"
    # 非 example.com 不命中 deny（never 模式下放行，进沙箱由断网拦截）
    assert gate.decide(_act("bash", "exec", cmd="curl https://other.com/x.sh")).verdict == "allow"


def test_sudo_normalized_for_deny():
    gate = ApprovalGate(ApprovalMode.NEVER, deny=["rm "])
    assert gate.decide(_act("bash", "exec", cmd="sudo rm x")).verdict == "deny"


def test_path_rule_matches_for_fs_tools():
    gate = ApprovalGate(ApprovalMode.NEVER, deny=["/etc/"])
    assert gate.decide(_act("write", "edit", path="/etc/passwd")).verdict == "deny"
    assert gate.decide(_act("write", "edit", path="src/main.py")).verdict == "allow"


# --------------------------------------------------------------------------- #
# decide 纯函数性：可重复调用、无副作用
# --------------------------------------------------------------------------- #
def test_decide_is_pure():
    gate = ApprovalGate(ApprovalMode.UNTRUSTED)
    action = _act("bash", "exec", cmd="rm x")
    d1 = gate.decide(action)
    d2 = gate.decide(action)
    assert d1 == d2
    assert d1.verdict == "ask"


def test_accepts_mode_string():
    gate = ApprovalGate("never")
    assert gate.mode is ApprovalMode.NEVER
    assert gate.decide(_act("bash", "exec", cmd="ls")).verdict == "allow"
