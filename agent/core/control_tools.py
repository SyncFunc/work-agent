"""控制工具（control tools）定义与按模式收集。

控制工具是 function-calling 形态、由循环解释的特殊工具，**不进入真实工具注册表**
（避免污染 M2 审批/沙箱对真实工具的枚举）：

- `ask_clarification`（M1.5）：模糊任务先提问，循环提前返回 `AgentResult(needs_clarification=True)`。
- `present_plan`（M1.4）：plan 模式下提交计划，循环落盘并提前返回。
- `update_plan`（M1.4）：执行期（**非 plan 模式**）更新步骤进度，循环回写计划文件。

所有 schema 集中在本文件，循环只调用 `collect_control_tools()` 按当前模式取用，
避免把工具定义散落进 `loop.py`。
"""

from __future__ import annotations

from typing import Any

from agent.config.settings import ClarifyConfig

# --------------------------------------------------------------------------- #
# 工具名常量（被各模块引用，避免硬编码字符串）
# --------------------------------------------------------------------------- #
ASK_CLARIFICATION_TOOL_NAME = "ask_clarification"
PRESENT_PLAN_TOOL_NAME = "present_plan"
UPDATE_PLAN_TOOL_NAME = "update_plan"

# --------------------------------------------------------------------------- #
# 各工具 OpenAI 兼容 schema（集中定义，单一事实来源）
# --------------------------------------------------------------------------- #
ASK_CLARIFICATION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": ASK_CLARIFICATION_TOOL_NAME,
        "description": (
            "当用户任务模糊、缺少关键信息或存在多种合理走向时，先向用户提问。"
            "不要在未澄清前执行任何操作。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "options": {"type": "array", "items": {"type": "string"}},
                            "multiSelect": {"type": "boolean"},
                        },
                        "required": ["question"],
                    },
                }
            },
            "required": ["questions"],
        },
    },
}

# M1.4 落地时启用；此处先集中定义，便于复用，不提前挂到循环。
PRESENT_PLAN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": PRESENT_PLAN_TOOL_NAME,
        "description": "PLAN 模式下调查完成后调用，提交计划。计划会落盘为文件供用户审阅。",
        "parameters": {
            "type": "object",
            "properties": {
                "body": {"type": "string", "description": "Markdown 计划正文：目标/方案/风险/文件清单"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "步骤稳定 id，如 S1"},
                            "title": {"type": "string"},
                            "detail": {"type": "string"},
                        },
                        "required": ["id", "title"],
                    },
                },
            },
            "required": ["body", "steps"],
        },
    },
}

UPDATE_PLAN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": UPDATE_PLAN_TOOL_NAME,
        "description": (
            "推进已批准计划时，标记某步状态。开始做前标记 in_progress，"
            "完成标记 done，遇阻标记 blocked，决定跳过标记 skipped。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "step_id": {"type": "string", "description": "present_plan 中给出的步骤 id，如 S2"},
                "status": {"type": "string", "enum": ["in_progress", "done", "blocked", "skipped"]},
                "note": {"type": "string"},
            },
            "required": ["step_id", "status"],
        },
    },
}


USE_SKILL_TOOL_NAME = "use_skill"
SPAWN_SUBAGENT_TOOL_NAME = "spawn_subagent"

USE_SKILL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": USE_SKILL_TOOL_NAME,
        "description": (
            "调用一个已注册的 Skill（按需加载其正文到上下文）。"
            "先参考系统提示中的「Available Skills」目录，用 name 触发。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "skill 名称（kebab-case）"},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "位置参数（替换正文中的 $ARGUMENTS / $N / $name）",
                },
            },
            "required": ["name"],
        },
    },
}

SPAWN_SUBAGENT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SPAWN_SUBAGENT_TOOL_NAME,
        "description": (
            "把一个子任务委派给独立上下文的子 agent，返回其摘要。"
            "适合大范围探索、并行调研或需要隔离上下文的任务。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "agent 类型（explore/plan/general-purpose 或自定义名）",
                },
                "task": {"type": "string", "description": "委派给子 agent 的任务描述"},
            },
            "required": ["agent", "task"],
        },
    },
}


def collect_control_tools(
    clarify: ClarifyConfig,
    *,
    plan_mode: bool = False,
    has_plan: bool = False,
    skills_enabled: bool = False,
    subagents_enabled: bool = False,
) -> list[dict]:
    """按当前模式收集应下发给模型的控制工具。

    规则（确定性，符合 98/1.6 法则）：
    - `ask_clarification`：随 `clarify.enabled` 并入。
    - `present_plan`：仅 **plan 模式**（`plan_mode=True`）并入。
    - `update_plan`：仅 **执行期**（`has_plan=True` 且 **非** plan 模式）并入——
      用于更新计划进度（M1.4 设计约束：update_plan 在「非 plan 模式」下使用）。
    - `use_skill`：仅当 `skills_enabled=True` 并入。
    - `spawn_subagent`：仅当 `subagents_enabled=True` 并入。
    """
    tools: list[dict] = []
    if clarify.enabled:
        tools.append(ASK_CLARIFICATION_TOOL)
    if plan_mode:
        tools.append(PRESENT_PLAN_TOOL)
    if has_plan and not plan_mode:
        tools.append(UPDATE_PLAN_TOOL)
    if skills_enabled:
        tools.append(USE_SKILL_TOOL)
    if subagents_enabled:
        tools.append(SPAWN_SUBAGENT_TOOL)
    return tools
