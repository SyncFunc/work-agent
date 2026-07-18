"""会话层（M1.6）：在多次 ``run`` 之间持有会话状态并编排一轮交互。

``AgentLoop`` 是无状态引擎（消息与模式均按 run 传入）；``Session`` 负责：
- 持有对话历史 ``messages``、澄清护栏计数 ``clarify_total``；
- 持有**按轮次可变**的模式状态 ``plan_mode`` / ``plan_path``（plan 探索 / exec 执行），
  用户可在任意轮次切换（由上层 CLI 改写本对象字段，再在下轮 ``step`` 传入 loop）；
- 在一轮 ``step`` 内编排：澄清回填、计划确认与模式切换。

**分层约束**：本模块位于 core，不依赖任何 CLI 框架（typer 等）。所有人机交互（提问、
确认、提示输出）与实时渲染通过注入的 ``AgentTransport`` 协议完成，CLI 层提供其具体实现
（``TerminalTransport``）；测试可注入假实现，无需真实 IO。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.core.loop import AgentLoop
from agent.core.model import Message
from agent.core.transport import AgentTransport

if TYPE_CHECKING:
    from agent.core.intent import Question
    from agent.core.loop import AgentResult


class Session:
    """会话状态持有者：对话历史、澄清护栏计数、当前 plan/exec 模式、已知计划文件。

    loop 本身无状态（消息与模式均按 run 传入），本类负责在多次 ``step`` 之间持有并续接。
    """

    def __init__(self, model, reg, settings, tracer=None, *, plan_mode: bool = False, plan_path=None):
        # 单个 AgentLoop 复用整段会话；模式/消息每次 run 显式传入，构造期缺省仅回落。
        self.loop = AgentLoop(model, reg, settings, tracer=tracer)
        self.settings = settings
        self.tracer = tracer
        self.messages: list[Message] = []
        self.clarify_total = 0
        self.plan_mode = plan_mode        # 当前模式（可在任意轮次切换）
        self.plan_path = plan_path        # 已知/已批准计划文件路径（触发 update_plan）

    async def step(
        self,
        task: str,
        transport: AgentTransport,
        *,
        yes: bool = False,
        fatal_plan_decline: bool = False,
    ) -> tuple["AgentResult", int | None]:
        """执行一轮（含澄清回填、plan 确认/切换）。返回 ``(res, err_code)``。

        - 澄清未解且非交互 → 通过 ``ui.show_questions`` 展示问题，返回 err_code=2（不静默跳过）。
        - 计划未确认：``fatal_plan_decline=True``（run）返回 err_code=1；否则（chat）留在 PLAN 模式继续。
        - 计划被确认 → 记录 plan_path、切 EXEC 模式、以原任务续跑（带已批准计划）。
        - 否则返回最终 res，err_code=None。
        - ``transport``：``AgentTransport`` 实例，承担 HITL 交互与实时事件渲染
          （``bind`` 订阅 ``EventStream``）；为 None 则无 UI、零渲染。
        """
        current_task = task
        while True:
            res = await self.loop.run(
                current_task,
                self.messages,
                clarify_total=self.clarify_total,
                plan_mode=self.plan_mode,
                plan_path=self.plan_path,
                transport=transport,
            )
            self.messages = list(res.messages or self.messages)
            self.clarify_total = res.clarify_total

            # ① 澄清回填（保持当前模式，用答案作为新任务再跑一轮）
            if res.needs_clarification:
                questions = res.questions or []
                if not transport.interactive:
                    transport.show_questions(questions)
                    return res, 2
                answers = [await transport.ask(q) for q in questions]
                current_task = "; ".join(
                    f"{q.question}: {a}" for q, a in zip(questions, answers)
                )
                continue

            # ② 计划确认 / 模式切换（仅 PLAN 模式且模型产出计划时）
            if res.needs_plan_confirm:
                transport.show_plan(res)
                # 立即记录已知计划（即便暂不批准），使随后的 EXEC 轮次能按 plan_path
                # 下发 update_plan 控制工具（M1.4：update_plan 仅在「非 plan 模式 + 已知计划」时可用）。
                self.plan_path = res.plan_path
                confirmed = yes or (transport.interactive and await transport.confirm_plan())
                if not confirmed:
                    if fatal_plan_decline:
                        transport.notify("计划未确认，已退出。")
                        return res, 1
                    transport.notify("计划未确认，保持 PLAN 模式。用 /exec 或 /approve 继续。")
                    return res, None
                # 批准：切 EXEC 模式，以原任务续跑（带已批准计划，启用 update_plan）
                self.plan_mode = False
                # 明确把「计划已批准、进入执行」写入对话历史：模型在批准前只见过
                # present_plan 的工具调用与回执，缺「用户已批准」信号会误以为仍在 PLAN
                # 模式、去查不存在的 .plan_status 等状态文件、甚至再次呈现计划（表现即
                # 「y/n 确认后仍没通过」）。此消息消除歧义，让模型直接进入执行。
                self.messages.append(Message(
                    role="user",
                    content=(
                        "[System] 上方的计划已经由用户确认通过，现在进入执行（EXEC）模式。"
                        "请直接按计划执行，用 update_plan 跟踪每步进度（in_progress→done）。"
                        "不要再次调用 present_plan，也不要去检查任何计划状态文件（如 .plan_status）。"
                    ),
                ))
                current_task = task
                continue

            # ③ 最终答案
            return res, None
