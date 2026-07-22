"""计划工件（plan artifact）：结构化计划 + 落盘文件。

设计要点（详见里程碑 1.4）：
- 计划是一个**可落盘、可重看、可续写**的工件（非仅内存字符串）。
- 步骤状态用「独立 JSON」存储（``plan.steps.json``），作为机器可读的**单一事实来源**：
  模型 / 循环更新步骤状态、CLI 展示进度都读它，避免 Markdown 复选框的脆弱解析。
- ``plan.md`` 保留人类可读正文（概述/方案/风险/文件清单），并附一份由 JSON 生成的
  ``## Steps`` 镜像，方便人直接打开查看进度（镜子，非来源）。
- ``PlanStore`` 负责渲染（Plan→文件）与解析（文件→Plan），``update_step`` 仅改对应
  步骤状态并重写 JSON（同步刷新 md 镜像）。
- 状态标记用 ASCII（跨平台/编码一致、易解析）：
  ``pending→[ ]`` / ``in_progress→[~]`` / ``done→[x]`` / ``blocked→[!]`` / ``skipped→[-]``。
- 本模块是纯数据 + 文件 IO，**不含循环编排逻辑**（编排在 loop.py）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# 步骤状态（字符串字面量集合）
PlanStatus = ("pending", "in_progress", "done", "blocked", "skipped")

# 状态 <-> Markdown 复选框标记（仅用于 md 镜像，JSON 才权威）
_STATUS_TO_MARK = {
    "pending": " ",
    "in_progress": "~",
    "done": "x",
    "blocked": "!",
    "skipped": "-",
}


@dataclass
class PlanStep:
    id: str  # 稳定标识，如 "S1"；update_plan 据此定位
    title: str
    status: str = "pending"  # ∈ PlanStatus
    detail: str | None = None


@dataclass
class Plan:
    body: str  # Markdown 正文：目标/方案/风险/文件清单
    steps: list[PlanStep] = field(default_factory=list)
    path: str | None = None  # 落盘路径（写后回填）


class PlanStore:
    """计划的持久化：写文件 / 读文件 / 单步状态更新。

    落盘布局（与 ``plan_file`` 同目录）：
    - ``plan.md``：正文 + 由 JSON 生成的 ``## Steps`` 镜像（人类可读）。
    - ``plan.steps.json``：步骤数组 ``[{id,title,status,detail}]``，机器可读、更新稳健。
    """

    @staticmethod
    def _steps_path(plan_path: str) -> Path:
        """步骤 JSON 路径：与 plan.md 同目录、同名换后缀，如 ``plan.steps.json``。"""
        p = Path(plan_path)
        return p.with_name(p.stem + ".steps.json")

    # ---- 渲染 --------------------------------------------------------------- #
    @staticmethod
    def _render_body(plan: Plan) -> str:
        """``plan.md`` 正文：用户 Markdown + 由 JSON 步骤生成的 ``## Steps`` 镜像。"""
        lines: list[str] = ["# Plan", "", plan.body.strip(), "", "## Steps", ""]
        for s in plan.steps:
            mark = _STATUS_TO_MARK.get(s.status, " ")
            lines.append(f"- [{mark}] {s.id} — {s.title}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_steps_json(plan: Plan) -> str:
        steps = [
            {"id": s.id, "title": s.title, "status": s.status, "detail": s.detail}
            for s in plan.steps
        ]
        return json.dumps(steps, ensure_ascii=False, indent=2) + "\n"

    @staticmethod
    def write_plan(plan: Plan, path: str) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(PlanStore._render_body(plan), encoding="utf-8")
        # 步骤权威存 JSON（与 md 同目录、同名换后缀）
        PlanStore._steps_path(path).write_text(PlanStore._render_steps_json(plan), encoding="utf-8")
        plan.path = str(p)
        return str(p)

    # ---- 解析 --------------------------------------------------------------- #
    @staticmethod
    def read_plan(path: str) -> Plan:
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        body = PlanStore._parse_body(text)
        steps = PlanStore._read_steps_json(path)
        if steps is None:
            # 兼容：JSON 缺失时回退从 md 的 ## Steps 解析（外部手改 / 旧文件）
            steps = PlanStore._parse_steps_from_md(text)
        return Plan(body=body, steps=steps, path=path)

    @staticmethod
    def _read_steps_json(plan_path: str) -> list[PlanStep] | None:
        sp = PlanStore._steps_path(plan_path)
        if not sp.exists():
            return None
        try:
            raw = json.loads(sp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return [
            PlanStep(
                id=s["id"],
                title=s.get("title", ""),
                status=s.get("status", "pending"),
                detail=s.get("detail"),
            )
            for s in raw
        ]

    @staticmethod
    def _parse_body(text: str) -> str:
        """从 md 提取正文（剥离首行 ``# Plan`` 标题与 ``## Steps`` 之后的内容）。"""
        body_lines: list[str] = []
        for line in text.splitlines():
            if line.strip().startswith("## Steps"):
                break
            body_lines.append(line)
        if body_lines and body_lines[0].strip() == "# Plan":
            body_lines = body_lines[1:]
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        return "\n".join(body_lines)

    @staticmethod
    def _parse_steps_from_md(text: str) -> list[PlanStep]:
        """兜底：从 md 的 ``## Steps`` 复选框还原步骤（JSON 缺失时）。"""
        steps: list[PlanStep] = []
        in_steps = False
        mark_to_status = {v: k for k, v in _STATUS_TO_MARK.items()}
        for line in text.splitlines():
            if line.strip().startswith("## Steps"):
                in_steps = True
                continue
            if not in_steps:
                continue
            stripped = line.strip()
            if not stripped.startswith("- ["):
                continue
            marker = stripped[3:4]
            rest = stripped[6:].strip()
            status = mark_to_status.get(marker, "pending")
            if " " in rest:
                sid, title = rest.split(" ", 1)
                sid = sid.rstrip("—-").strip() or sid
                title = title.lstrip("—- ").strip()
            else:
                sid, title = rest, ""
            steps.append(PlanStep(id=sid, title=title, status=status))
        return steps

    # ---- 单步更新 ----------------------------------------------------------- #
    @staticmethod
    def update_step(path: str, step_id: str, status: str, note: str | None = None) -> Plan:
        """把指定步骤状态更新为 ``status``（note 可选，附到标题后），重写 JSON 后返回最新 Plan。"""
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
        # 重写 JSON（权威）+ 刷新 md 镜像
        PlanStore.write_plan(plan, path)
        return plan
