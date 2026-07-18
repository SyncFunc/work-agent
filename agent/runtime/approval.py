"""审批门（ApprovalGate，M2.2）：确定性"这次工具调用能不能跑"决策组件。

设计依据：[`../../knowledge/sandbox-approval-design.md`](../../knowledge/sandbox-approval-design.md) §3。
采用 Codex 的 ``AskForApproval`` 四模式 + allow/deny 声明式规则 + 单步 HITL 回调。

核心思想（与 PLAN 模式正交的纵深第二道闸）：
- 审批门**不做命令执行**，只回答"ALLOW / DENY / ASK"。
- 决策顺序铁律：``deny``(1) > ``escalated``(2) > ``read 非 untrusted``(3) > ``allow``(4) > ``mode``(5)。
- 安全不变量：``deny`` 规则永远优先于一切模式（含 ``never`` 模式）；``escalated`` 提权无视 mode 强制 ASK。
- 感知沙箱：``decide`` 接收 ``sandbox_profile``（包含程度信号）；当前决策逻辑不因其改变 verdict
  （profile 在执行时由沙箱层 OS 强制隔离，见 M2.1），但签名保留以便 M2.4 增强。
- HITL 解耦：``authorize`` 仅在 ASK 分支 ``await ui.approve(action)``；``ui`` 为 ``None``
  （非交互/测试）时按 ``noninteractive_default``（默认 allow）放行，不阻塞 CI。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

# 命令分段正则（与 bash.is_readonly_command 同思路：按 ; && || | 切多段）
_SPLIT_RE = re.compile(r"\s*(?:;|\|\||&&|\|)\s*")


def _normalize_cmd(cmd: str) -> list[str]:
    """把整条命令归一化为可匹配的命令段列表。

    - 按 ``; && || |`` 切段；
    - 每段去掉段首环境变量赋值（``KEY=VALUE cmd``）与 ``sudo``/``doas`` 前缀，
      使 ``sudo rm x`` 能被 ``rm `` 规则命中（验收要求）。
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
        # 去 sudo/doas 前缀（提权关键字本身不影响规则匹配目标）
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


def _match_text(action: "Action") -> list[str]:
    """取出用于规则匹配的文本片段：bash→命令段；路径工具→path；兜底→args 值拼接。"""
    args = action.args
    cmd = args.get("cmd")
    if cmd is not None:
        return _normalize_cmd(str(cmd))
    path = args.get("path")
    if path is not None:
        return [str(path)]
    return [str(v) for v in args.values() if v is not None]


def _match(action: "Action", rules: list[str]) -> bool:
    if not rules:
        return False
    return any(_rule_matches(text, rules) for text in _match_text(action))


class ApprovalMode(str, Enum):
    """审批四模式（Codex AskForApproval 同构）。低风险偏好旋钮，不替代 deny 安全网。"""

    UNTRUSTED = "untrusted"   # exec/edit 每步问；read 自动过（默认安全档）
    ON_REQUEST = "on-request"  # 自动跑；模型对单条命令标 approval_request 才问（LLM 决定）
    ON_FAILURE = "on-failure"  # 自动跑；失败才问
    NEVER = "never"            # 全自动；deny 仍生效


@dataclass
class Action:
    """一次待裁决的工具调用。由 M2.4 的 loop 在执行每个工具前构造。"""

    tool: str                  # bash / read / write / edit ...
    risk: str                  # read / edit / exec（来自 registry.risk）
    args: dict[str, Any]       # 命令文本 / 路径，供规则匹配
    description: str           # 人类可读一行，给 HITL 展示
    approval_request: bool = False  # 模型在单条命令显式请求审批（on-request 模式用）
    escalated: bool = False         # 提权操作（装包 / 改系统配置），触发强制 ASK


@dataclass
class Decision:
    """裁决结果。verdict ∈ {allow, deny, ask}。"""

    verdict: str
    reason: str


@runtime_checkable
class ApprovalUI(Protocol):
    """HITL 回调契约（M2.5 在 AgentTransport 上实现 approve）。gate 不持有 IO，只调回调。"""

    async def approve(self, action: Action) -> bool:
        """返回 True 放行、False 拒绝。"""
        ...


class ApprovalGate:
    """确定性审批决策组件：只回答"这次工具调用能不能跑"。

    - ``decide`` 是纯函数（无副作用、可重复调用），供测试与可观测性直接断言。
    - ``authorize`` 仅在 ASK 分支 ``await ui.approve``；无 ui 时按 ``noninteractive_default`` 放行。
    """

    def __init__(
        self,
        mode: "ApprovalMode | str",
        *,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
        ui: "ApprovalUI | None" = None,
        noninteractive_default: str = "allow",
        sandbox_profile: str = "workspace-write",
    ) -> None:
        self.mode = mode if isinstance(mode, ApprovalMode) else ApprovalMode(mode)
        self.allow = list(allow or [])
        self.deny = list(deny or [])
        self.ui = ui
        # ASK 且无 HITL 回调时的默认放行策略（默认 allow，因你已委派任务且命令进沙箱）
        self.noninteractive_default = noninteractive_default
        # 感知沙箱：作为 decide 的"包含程度"信号（当前不改变 verdict，M2.4 可增强）
        self.sandbox_profile = sandbox_profile

    def decide(self, action: Action, sandbox_profile: str | None = None) -> Decision:
        """纯函数裁决。``sandbox_profile`` 省略时回退构造期默认值。

        决策顺序（铁律）：
          1) deny 命中 → DENY（安全不变量，覆盖一切模式）
          2) escalated 提权 → ASK（无视 mode）
          3) read 且非 untrusted → ALLOW
          4) allow 命中 → ALLOW（短路，跳过 ASK，不解除沙箱）
          5) 按 mode：untrusted→(exec/edit)ASK/(read)ALLOW；
             on-request→approval_request?ASK:ALLOW；on-failure→ALLOW；never→ALLOW
        """
        # 1) deny 永远优先
        if _match(action, self.deny):
            return Decision("deny", "命中 deny 规则（安全网，覆盖一切模式）")
        # 2) 提权操作无视模式强制 ASK
        if action.escalated:
            return Decision("ask", "提权操作，需人工确认")
        # 3) 只读且非 untrusted → 自动放行（只读不危险）
        if action.risk == "read" and self.mode != ApprovalMode.UNTRUSTED:
            return Decision("allow", "只读操作，自动放行")
        # 4) allow 命中 → 短路 ALLOW
        if _match(action, self.allow):
            return Decision("allow", "命中 allow 规则，自动放行")
        # 5) 按模式
        if self.mode == ApprovalMode.UNTRUSTED:
            if action.risk in ("edit", "exec"):
                return Decision("ask", "untrusted 模式：写/执行需确认")
            return Decision("allow", "untrusted 模式：只读放行")
        if self.mode == ApprovalMode.ON_REQUEST:
            if action.approval_request:
                return Decision("ask", "模型显式请求审批")
            return Decision("allow", "on-request 模式：默认放行")
        if self.mode == ApprovalMode.ON_FAILURE:
            return Decision("allow", "on-failure 模式：失败才问")
        # NEVER（仍受 deny 约束，已在第 1 步拦截）
        return Decision("allow", "never 模式：全自动（仍受 deny 约束）")

    async def authorize(self, action: Action) -> bool:
        """返回 True 放行 / False 拒绝。仅在 ASK 分支 await ui。"""
        d = self.decide(action)
        if d.verdict == "deny":
            return False
        if d.verdict == "allow":
            return True
        # ASK：有 HITL 回调则询问，否则按非交互默认放行
        if self.ui is None:
            return self.noninteractive_default == "allow"
        return bool(await self.ui.approve(action))
