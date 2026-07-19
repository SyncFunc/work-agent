"""会话层（M1.6）：在多次 ``run`` 之间持有会话状态并编排一轮交互。

M3.1 增强：集成 ``TraceStore`` 实现 trace 持久化，每轮 step 结束自动保存。
M3.3 增强：集成 Pipeline 保护 Sandbox 调用。
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

from agent.core.loop import AgentLoop
from agent.core.model import Message
from agent.core.transport import AgentTransport
from agent.obs.store import TraceStore
from agent.obs.tracer import Span

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
        # 会话级根 span：整条 session 的 trace 树锚点（所有 run 都挂在其下）。
        # tracer 为 None 时不创建（无观测路径）；否则直接 new Span 并挂到 tracer，
        # 生命周期跨多轮，不用 _span 上下文管理器（避免 per-step 关闭）。
        self.root_span: Span | None = None
        if tracer is not None:
            self.root_span = Span(
                id=uuid.uuid4().hex[:8],
                name="session",
                kind="session",
                parent_id=None,
                started_at=time.time(),
            )
            tracer.spans.append(self.root_span)
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

        # M5.3：构造 SkillLoader / SubagentSpawner 并接入 AgentLoop
        import os

        from agent.skills.loader import SkillLoader
        from agent.subagent import SubagentSpawner

        skill_loader = None
        subagent_spawner = None
        if settings.skills.enabled:
            _proj = Path(os.environ.get("AGENT_PROJECT_ROOT") or Path.cwd())
            skill_loader = SkillLoader(_proj)
        if settings.subagents.enabled:
            subagent_spawner = SubagentSpawner(
                settings, tracer=tracer, max_depth=settings.subagents.max_depth
            )
        self.loop = AgentLoop(
            model, reg, settings, tracer=tracer, sandbox=sandbox, gate=gate,
            skill_loader=skill_loader, subagent_spawner=subagent_spawner,
        )
        # M5.4：持有 loader/spawner 供 CLI 命令（/skills /agents /skill）直接查询
        self.skill_loader = skill_loader
        self.subagent_spawner = subagent_spawner
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
                parent_span=self.root_span,
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
            # 落盘时把根 span 的结束时间推进到当前（反映累计会话时长）
            if self.root_span is not None and self.root_span.ended_at is None:
                self.root_span.ended_at = time.time()
            self.trace_store.save_trace(self.tracer)

    # ------------------------------------------------------------------ #
    # M5.4：CLI 查询辅助（实时重扫：summaries() 内部重新 discover()）
    # ------------------------------------------------------------------ #
    def list_skills(self) -> list:
        """已注册 Skill 精简列表；未启用返回 []。"""
        if self.skill_loader is None:
            return []
        return self.skill_loader.summaries()

    def list_agents(self) -> list:
        """已注册 Subagent 类型精简列表；未启用返回 []。"""
        if self.subagent_spawner is None:
            return []
        return self.subagent_spawner.summaries()
