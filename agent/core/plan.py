"""计划工件（plan artifact）：结构化计划 + 落盘文件。

设计要点（详见里程碑 1.4）：
- 计划是一个**可落盘、可重看、可续写**的工件（非仅内存字符串）。
- ``Plan`` 含 Markdown 正文 + 结构化步骤列表（每步稳定 ``id`` + ``title`` + ``status``）。
- ``PlanStore`` 负责渲染（Plan→文件）与解析（文件→Plan），``update_step`` 仅改对应步骤状态并重写文件。
- 状态标记用 ASCII（跨平台/编码一致、易解析）：
  ``pending→[ ]`` / ``in_progress→[~]`` / ``done→[x]`` / ``blocked→[!]`` / ``skipped→[-]``。
- 本模块是纯数据 + 文件 IO，**不含循环编排逻辑**（编排在 loop.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


# 步骤状态（字符串字面量集合）
PlanStatus = ("pending", "in_progress", "done", "blocked", "skipped")

# 状态 <-> Markdown 复选框标记
_STATUS_TO_MARK = {
    "pending": " ",
    "in_progress": "~",
    "done": "x",
    "blocked": "!",
    "skipped": "-",
}
_MARK_TO_STATUS = {v: k for k, v in _STATUS_TO_MARK.items()}


@dataclass
class PlanStep:
    id: str                     # 稳定标识，如 "S1"；update_plan 据此定位
    title: str
    status: str = "pending"     # ∈ PlanStatus
    detail: str | None = None


@dataclass
class Plan:
    body: str                   # Markdown 正文：目标/方案/风险/文件清单
    steps: list[PlanStep] = field(default_factory=list)
    path: str | None = None     # 落盘路径（写后回填）


class PlanStore:
    """计划的持久化：写文件 / 读文件 / 单步状态更新。"""

    @staticmethod
    def _render(plan: Plan) -> str:
        lines: list[str] = ["# Plan", "", plan.body.strip(), "", "## Steps", ""]
        for s in plan.steps:
            mark = _STATUS_TO_MARK.get(s.status, " ")
            lines.append(f"- [{mark}] {s.id} — {s.title}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def write_plan(plan: Plan, path: str) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(PlanStore._render(plan), encoding="utf-8")
        plan.path = str(p)
        return str(p)

    @staticmethod
    def read_plan(path: str) -> Plan:
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        return PlanStore._parse(text, path=str(p))

    @staticmethod
    def _parse(text: str, path: str | None = None) -> Plan:
        lines = text.splitlines()
        # 正文 = 从首行之后到 "## Steps" 之前的全部内容
        body_lines: list[str] = []
        steps: list[PlanStep] = []
        in_steps = False
        for line in lines:
            if line.strip().startswith("## Steps"):
                in_steps = True
                continue
            if in_steps:
                stripped = line.strip()
                if not stripped.startswith("- ["):
                    continue
                # 形如 "- [x] S1 — 标题"（- [ ] 占 0..5，内容从 index 6 起）
                marker = stripped[3:4]
                rest = stripped[6:].strip()
                status = _MARK_TO_STATUS.get(marker, "pending")
                if " " in rest:
                    sid, title = rest.split(" ", 1)
                    sid = sid.rstrip("—-").strip() or sid
                    title = title.lstrip("—- ").strip()
                else:
                    sid, title = rest, ""
                steps.append(PlanStep(id=sid, title=title, status=status))
            else:
                body_lines.append(line)
        # 渲染时首行为 "# Plan" 标题，解析时剥离，仅保留用户正文
        if body_lines and body_lines[0].strip() == "# Plan":
            body_lines = body_lines[1:]
        # 去掉首尾空行
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        return Plan(body="\n".join(body_lines), steps=steps, path=path)

    @staticmethod
    def update_step(
        path: str, step_id: str, status: str, note: str | None = None
    ) -> Plan:
        """把指定步骤状态更新为 ``status``（note 可选，附到标题后），重写文件后返回最新 Plan。"""
        if status not in PlanStatus:
            raise ValueError(f"invalid plan status: {status!r}; expected one of {PlanStatus}")
        plan = PlanStore.read_plan(path)
        found = False
        for s in plan.steps:
            if s.id == step_id:
                s.status = status
                if note:
                    s.title = f"{s.title}  ({note})"
                found = True
                break
        if not found:
            raise KeyError(f"plan step not found: {step_id!r}")
        PlanStore.write_plan(plan, path)
        return plan
