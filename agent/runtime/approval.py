"""审批门（ApprovalGate，M2.2）：确定性"这次工具调用能不能跑"决策组件。

设计依据：[`../../knowledge/sandbox-approval-design.md`](../../knowledge/sandbox-approval-design.md) §3。
采用 Codex 的 ``AskForApproval`` 四模式 + 执行策略（exec_policy）+ 单步 HITL 回调。

核心简化（M2 重构后）：
- 去掉 deny 规则：安全由沙箱层（OS/CommandFilter）保障，审批层不重复。
- 去掉 escalated 强制 ASK：提权操作不再独立触发审批，交给模式自身逻辑。
- 提权不再需要开关（enable_elevation）：只要命令经 ASK→批准且需越权（联网），
  即自动以 elevated_profile 临时执行。approval = permission to break sandbox。
- exec_policy（执行策略）：仅 ``unless-trusted`` 模式有效，命中者免审直接 ALLOW。
  on-request/on-failure/never 模式忽略 exec_policy。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from agent.runtime.sandbox import SandboxProfile, analyze_command

if TYPE_CHECKING:
    from agent.core.transport import AgentTransport

# 命令分段正则（与 bash.is_readonly_command 同思路：按 ; && || | 切多段）
_SPLIT_RE = re.compile(r"\s*(?:;|\|\||&&|\|)\s*")


def _normalize_cmd(cmd: str) -> list[str]:
    """把整条命令归一化为可匹配的命令段列表。

    - 按 ``; && || |`` 切段；
    - 每段去掉段首环境变量赋值（``KEY=VALUE cmd``）与 ``sudo``/``doas`` 前缀。
    """
    segs: list[str] = []
    for seg in _SPLIT_RE.split(cmd.strip()):
        seg = seg.strip()
        if not seg:
            continue
        # 去段首环境变量赋值
        while True:
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=\S+\s+(.*)$", seg)
            if not m:
                break
            seg = m.group(2).strip()
        if not seg:
            continue
        # 去 sudo/doas 前缀
        if seg.startswith("sudo ") or seg.startswith("doas "):
            seg = seg.split(" ", 1)[1].strip()
        if seg:
            segs.append(seg)
    return segs


def _rule_matches(text: str, rules: list[str]) -> bool:
    """单段文本对规则列表的匹配：正则（``/.../`` 包裹）用 re.search，否则前缀匹配。"""
    for rule in rules:
        if not rule:
            continue
        if len(rule) >= 2 and rule.startswith("/") and rule.endswith("/"):
            if re.search(rule[1:-1], text):
                return True
        elif text.startswith(rule):
            return True
    return False


def _match_text(action: Action) -> list[str]:
    """取出用于规则匹配的文本片段：bash→命令段；路径工具→path；兜底→args 值拼接。"""
    args = action.args
    cmd = args.get("cmd")
    if cmd is not None:
        return _normalize_cmd(str(cmd))
    path = args.get("path")
    if path is not None:
        return [str(path)]
    return [str(v) for v in args.values() if v is not None]


def _match(action: Action, rules: list[str]) -> bool:
    if not rules:
        return False
    return any(_rule_matches(text, rules) for text in _match_text(action))


class ApprovalMode(StrEnum):
    """审批四模式（Codex AskForApproval 同构）。"""

    ON_REQUEST = "on-request"  # 官方推荐。默认放行；模型标 approval_request 才 ASK
    UNLESS_TRUSTED = "unless-trusted"  # exec/edit 每步问，exec_policy 命中者免审；read 自动过
    ON_FAILURE = "on-failure"  # 先执行，失败才问
    NEVER = "never"  # 永远不请求审批；权限不足→直接失败


@dataclass
class Action:
    """一次待裁决的工具调用。由 loop 在执行每个工具前构造。"""

    tool: str  # bash / read / write / edit ...
    risk: str  # read / edit / exec（来自 registry.risk）
    args: dict[str, Any]  # 命令文本 / 路径，供规则匹配
    description: str  # 人类可读一行，给 HITL 展示
    approval_request: bool = False  # 模型在单条命令显式请求审批（on-request 模式用）


@dataclass
class Decision:
    """裁决结果。verdict ∈ {allow, ask}。

    ``elevated_profile``：当命令被批准且需要当前 profile 不允许的能力（如联网）时，
    携带应临时提升到的目标档位；否则为 ``None``。loop 据此把该次执行以
    ``elevated_profile`` 跑（用完即回受限）。提权自动生效，不再需要开关。
    """

    verdict: str
    reason: str
    elevated_profile: SandboxProfile | None = None


class ApprovalGate:
    """确定性审批决策组件：只回答"这次工具调用能不能跑"。

    - ``decide`` 是纯函数（无副作用、可重复调用）。
    - ``authorize`` 仅在 ASK 分支 ``await transport.approve``；无 transport 时按 ``noninteractive_default`` 放行。
      注意：``authorize`` 不再接收构造期注入的 ui，而是由调用方（loop）在运行时传入 transport，
      因为 ``AgentTransport`` 是 loop 运行期才绑定的，gate 在构造期不持有它。
    - 提权自动生效：只要命令经 ASK→批准且需越权（联网），即自动携带 elevated_profile。
    """

    def __init__(
        self,
        mode: ApprovalMode | str,
        *,
        exec_policy: list[str] | None = None,
        noninteractive_default: str = "allow",
        sandbox_profile: str | SandboxProfile = "workspace-write",
        elevated_profile: str | SandboxProfile = SandboxProfile.DANGER_FULL,
    ) -> None:
        self.mode = mode if isinstance(mode, ApprovalMode) else ApprovalMode(mode)
        self.exec_policy = list(exec_policy or [])
        self.noninteractive_default = noninteractive_default
        self.sandbox_profile = (
            sandbox_profile
            if isinstance(sandbox_profile, SandboxProfile)
            else SandboxProfile(sandbox_profile)
        )
        self.elevated_profile = (
            elevated_profile
            if isinstance(elevated_profile, SandboxProfile)
            else SandboxProfile(elevated_profile)
        )

    def decide(
        self, action: Action, sandbox_profile: str | SandboxProfile | None = None
    ) -> Decision:
        """纯函数裁决。``sandbox_profile`` 省略时回退构造期默认值。

        决策顺序（精简后 3 步）：
          1) read 且非 unless-trusted → ALLOW（只读自动放行）
          2) unless-trusted 模式 + exec_policy 命中 → ALLOW（免审，仅 unless-trusted 有效）
          3) 按 mode：
             on-request → approval_request? ASK : ALLOW
             unless-trusted → exec/edit ASK, read ALLOW
             on-failure → ALLOW（先执行，失败再问补救）
             never → ALLOW（永不问，不足直接失败）

        提权（自动）：**仅当 verdict=="ask"（需经批准）** 且命令需要当前 profile 不允许
        的能力（写 / 联网）时，在 ``Decision.elevated_profile`` 携带计算后的目标档
        （``workspace-write`` 或 ``danger-full``）。普通 ALLOW 不提权（失败走 on-failure，
        模型从错误学习）。
        """
        sp = (
            sandbox_profile
            if isinstance(sandbox_profile, SandboxProfile)
            else (SandboxProfile(sandbox_profile) if sandbox_profile else self.sandbox_profile)
        )

        # 1) 只读且非 unless-trusted → 自动放行（只读不危险）
        if action.risk == "read" and self.mode != ApprovalMode.UNLESS_TRUSTED:
            verdict, reason = "allow", "只读操作，自动放行"
        # 2) unless-trusted 模式 + exec_policy 命中 → 免审
        elif self.mode == ApprovalMode.UNLESS_TRUSTED and _match(action, self.exec_policy):
            verdict, reason = "allow", "unless-trusted 模式：exec_policy 命中，自动放行"
        # 3) 按模式
        elif self.mode == ApprovalMode.ON_REQUEST:
            if action.approval_request:
                verdict, reason = "ask", "模型显式请求审批"
            else:
                verdict, reason = "allow", "on-request 模式：默认放行"
        elif self.mode == ApprovalMode.UNLESS_TRUSTED:
            if action.risk in ("edit", "exec"):
                verdict, reason = "ask", "unless-trusted 模式：写/执行需确认"
            else:
                verdict, reason = "allow", "unless-trusted 模式：只读放行"
        elif self.mode == ApprovalMode.ON_FAILURE:
            verdict, reason = "allow", "on-failure 模式：失败才问"
        else:  # NEVER
            verdict, reason = "allow", "never 模式：全自动"

        # 提权自动计算：仅当 verdict=="ask"（需批准）且命令确需越权
        elevated: SandboxProfile | None = None
        if verdict == "ask":
            needs, target = self._check_elevation(action, sp)
            if needs:
                elevated = target
        return Decision(verdict, reason, elevated_profile=elevated)

    @staticmethod
    def _check_elevation(action: Action, sp: SandboxProfile) -> tuple[bool, SandboxProfile | None]:
        """检查命令是否需要提权，若需要则同时返回目标档位。

        批准即 permission to break sandbox：用户已经放行，所以当前 profile 不允许的
        能力（写 / 联网）都应自动提权。

        返回 ``(needs_elevation, target_profile)``：
        - ``needs_elevation=False`` → 不提权，target 为 ``None``
        - ``needs_elevation=True`` → 提权到 ``target``：
          * 联网 → ``danger-full``
          * 只写（read-only 下）→ ``workspace-write``
        """
        if action.tool != "bash":
            return False, None
        cmd = action.args.get("cmd")
        if not cmd:
            return False, None
        has_network, write_targets = analyze_command(cmd)
        if sp == SandboxProfile.DANGER_FULL:
            return False, None
        if has_network:
            return True, SandboxProfile.DANGER_FULL
        if sp == SandboxProfile.READ_ONLY and write_targets:
            return True, SandboxProfile.WORKSPACE_WRITE
        return False, None

    async def authorize(self, action: Action, transport: AgentTransport | None = None) -> bool:
        """返回 True 放行 / False 拒绝。仅在 ASK 分支 await transport.approve。

        ``transport`` 由调用方（loop）在运行时传入，gate 不持有它。
        无 transport 时按 ``noninteractive_default`` 放行（非交互 / 测试场景）。
        """
        d = self.decide(action)
        if d.verdict == "allow":
            return True
        # ASK：有 HITL 回调则询问，否则按非交互默认放行
        if transport is None:
            return self.noninteractive_default == "allow"
        return bool(await transport.approve(action))
