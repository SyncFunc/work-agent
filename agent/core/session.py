"""会话层（M1.6）：在多次 ``run`` 之间持有会话状态并编排一轮交互。

M3.1 增强：集成 ``TraceStore`` 实现 trace 持久化，每轮 step 结束自动保存。
M3.3 增强：集成 Pipeline 保护 Sandbox 调用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.core.loop import AgentLoop
from agent.core.model import Message
from agent.core.transport import AgentTransport
from agent.obs.store import TraceStore

if TYPE_CHECKING:
    from agent.core.intent import Question
    from agent.core.loop import AgentResult


class Session:
    def __init__(self, model, reg, settings, tracer=None, *, plan_mode: bool = False, plan_path=None, trace_store=None):
        from pathlib import Path

        from agent.resilience.pipeline import build_sandbox_pipeline
        from agent.runtime.approval import ApprovalGate
        from agent.runtime.sandbox import SandboxProfile, build_executor

        self.settings = settings
        self.tracer = tracer
        self.trace_store: TraceStore | None = trace_store
        sandbox_pipeline = build_sandbox_pipeline(settings)
        sandbox = build_executor(
            settings.sandbox.mode,
            workspace=Path.cwd(),
            profile=SandboxProfile(settings.sandbox.profile),
            pipeline=sandbox_pipeline,
        )
        gate = ApprovalGate(
            settings.approval.mode,
            exec_policy=settings.approval.exec_policy,
            noninteractive_default=settings.approval.noninteractive_default,
            sandbox_profile=SandboxProfile(settings.sandbox.profile),
            elevated_profile=SandboxProfile(settings.approval.elevated_sandbox_profile),
        )
        self.loop = AgentLoop(model, reg, settings, tracer=tracer, sandbox=sandbox, gate=gate)
        self.messages: list[Message] = []
        self.clarify_total = 0
        self.plan_mode = plan_mode
        self.plan_path = plan_path

    async def step(
        self,
        task: str,
        transport: AgentTransport,
        *,
        yes: bool = False,
        fatal_plan_decline: bool = False,
    ) -> tuple["AgentResult", int | None]:
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

            # 每轮 step 结束自动持久化 trace（若有 trace_store）
            self._save_trace()

            # ① 澄清回填
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

            # ② 计划确认 / 模式切换
            if res.needs_plan_confirm:
                transport.show_plan(res)
                self.plan_path = res.plan_path
                confirmed = yes or (transport.interactive and await transport.confirm_plan())
                if not confirmed:
                    if fatal_plan_decline:
                        transport.notify("计划未确认，已退出。")
                        return res, 1
                    transport.notify("计划未确认，保持 PLAN 模式。用 /exec 或 /approve 继续。")
                    return res, None
                self.plan_mode = False
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

    def _save_trace(self) -> None:
        if self.tracer is not None and self.trace_store is not None:
            self.trace_store.save_trace(self.tracer)
