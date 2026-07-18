"""会话层（M1.6）：在多次 ``run`` 之间持有会话状态并编排一轮交互。

``AgentLoop`` 是无状态引擎（消息与模式均按 run 传入）；``Session`` 负责：
- 持有对话历史 ``messages``、澄清护栏计数 ``clarify_total``；
- 持有**按轮次可变**的模式状态 ``plan_mode`` / ``plan_path``（plan 探索 / exec 执行），
  用户可在任意轮次切换（由上层 CLI 改写本对象字段，再在下轮 ``step`` 传入 loop）；
- 在一轮 ``step`` 内编排：澄清回填、计划确认与模式切换。

**分层约束**：本模块位于 core，不依赖任何 CLI 框架（typer 等）。所有人机交互（提问、
确认、提示输出）通过注入的 ``SessionUI`` 协议完成，CLI 层提供其具体实现。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from agent.core.loop import AgentLoop

if TYPE_CHECKING:
    from agent.core.intent import Question
    from agent.core.loop import AgentResult
    from agent.core.model import Message
    from agent.core.presenter import LoopPresenter


@runtime_checkable
class SessionUI(Protocol):
    """会话交互协议：把人机交互（IO）与会话编排逻辑解耦。

    CLI 层实现本协议（typer 封装）；测试可注入假实现驱动分支，无需真实 IO。
    """

    @property
    def interactive(self) -> bool:
        """当前是否处于可交互环境（可向用户提问/确认）。"""
        ...

    async def ask(self, question: "Question") -> str:
        """向用户提出一条澄清问题并返回其答案（仅交互环境调用）。"""
        ...

    def show_questions(self, questions: list["Question"]) -> None:
        """非交互环境下，展示无法回填的澄清问题（随后会以 err_code=2 退出）。"""
        ...

    def show_plan(self, res: "AgentResult") -> None:
        """展示模型产出的计划（正文 + 步骤 + 计划文件路径）。"""
        ...

    def confirm_plan(self) -> bool:
        """询问用户是否执行当前计划（仅交互环境调用）。"""
        ...

    def notify(self, message: str) -> None:
        """输出一条提示/状态信息（非最终答案，如模式切换、计划未确认等）。"""
        ...


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
        ui: SessionUI,
        *,
        yes: bool = False,
        fatal_plan_decline: bool = False,
        presenter: "LoopPresenter | None" = None,
    ) -> tuple["AgentResult", int | None]:
        """执行一轮（含澄清回填、plan 确认/切换）。返回 ``(res, err_code)``。

        - 澄清未解且非交互 → 通过 ``ui.show_questions`` 展示问题，返回 err_code=2（不静默跳过）。
        - 计划未确认：``fatal_plan_decline=True``（run）返回 err_code=1；否则（chat）留在 PLAN 模式继续。
        - 计划被确认 → 记录 plan_path、切 EXEC 模式、以原任务续跑（带已批准计划）。
        - 否则返回最终 res，err_code=None。
        - ``presenter``：ReAct 循环内部实时事件的渲染器（流式文本/思考/工具调用）；为 None 则静默。
        """
        current_task = task
        while True:
            res = await self.loop.run(
                current_task,
                self.messages,
                clarify_total=self.clarify_total,
                plan_mode=self.plan_mode,
                plan_path=self.plan_path,
                presenter=presenter,
            )
            self.messages = list(res.messages or self.messages)
            self.clarify_total = res.clarify_total

            # ① 澄清回填（保持当前模式，用答案作为新任务再跑一轮）
            if res.needs_clarification:
                questions = res.questions or []
                if not ui.interactive:
                    ui.show_questions(questions)
                    return res, 2
                answers = [await ui.ask(q) for q in questions]
                current_task = "; ".join(
                    f"{q.question}: {a}" for q, a in zip(questions, answers)
                )
                continue

            # ② 计划确认 / 模式切换（仅 PLAN 模式且模型产出计划时）
            if res.needs_plan_confirm:
                ui.show_plan(res)
                confirmed = yes or (ui.interactive and ui.confirm_plan())
                if not confirmed:
                    if fatal_plan_decline:
                        ui.notify("计划未确认，已退出。")
                        return res, 1
                    ui.notify("计划未确认，保持 PLAN 模式。用 /exec 或 /approve 继续。")
                    return res, None
                # 批准：记录计划、切 EXEC 模式，以原任务续跑（带已批准计划，启用 update_plan）
                self.plan_path = res.plan_path
                self.plan_mode = False
                current_task = task
                continue

            # ③ 最终答案
            return res, None
