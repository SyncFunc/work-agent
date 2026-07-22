"""意图澄清（M1.5）：模糊任务先问后做。

设计要点（详见 milestones/M1-骨架/1.5-意图澄清.md）：
- `ask_clarification` 是一个**控制工具**（function-calling 形态），其 schema 定义在
  `agent/core/control_tools.py`（集中管理，与循环解耦）。本模块只负责**解析 Decision**
  与**问题数据结构**，不内联工具定义。
- 本模块是**纯解析 + 数据结构**：无 IO、不调用模型；`loop.py` 只负责「识别 → 提前返回」编排。
- `Question` 结构化但宽松：`question` 必填，`options`/`multiSelect` 可选，降低用户认知负担。

控制工具名常量从 `control_tools` 复用（单一事实来源），此处再导出以便既有 import 不变。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.core.control_tools import ASK_CLARIFICATION_TOOL_NAME  # 单一事实来源

# 保持既有 import 路径兼容（tests 从 intent 导入该名）
__all__ = ["Question", "extract_clarify", "ASK_CLARIFICATION_TOOL_NAME"]


@dataclass
class Question:
    """一条澄清问题。`question` 必填；`options` 给候选项（可空）；`multiSelect` 多选开关。"""

    question: str
    options: list[str] | None = None
    multiSelect: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"question": self.question}
        if self.options is not None:
            d["options"] = self.options
        if self.multiSelect:
            d["multiSelect"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Question:
        return cls(
            question=d["question"],
            options=d.get("options"),
            multiSelect=bool(d.get("multiSelect", False)),
        )


def extract_clarify(decision: Any) -> list[Question] | None:
    """从 Decision 提取 `ask_clarification` 调用。

    - 若同轮 Decision 混有 `ask_clarification` 与其它工具调用：**澄清优先**，直接返回问题，
      调用方应提前返回（忽略同轮其它调用）。
    - 若 `ask_clarification` 的 `questions` 为空/缺失，视为无效调用，返回 None（让循环继续）。
    - 若 Decision 不含该工具，返回 None。
    """
    for tc in decision.tool_calls:
        if tc.name == ASK_CLARIFICATION_TOOL_NAME:
            raw = tc.arguments.get("questions", [])
            questions = [
                Question.from_dict(q) for q in raw if isinstance(q, dict) and q.get("question")
            ]
            return questions or None
    return None
