"""会话层（M1.6）：在多次 ``run`` 之间持有会话状态并编排一轮交互。

M3.1 增强：集成 ``TraceStore`` 实现 trace 持久化，每轮 step 结束自动保存。
M3.3 增强：集成 Pipeline 保护 Sandbox 调用。
M5.4 增强：后台 Subagent 支持——``spawn_background()`` 启动异步任务，完成后回填摘要。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING

from agent.core.loop import AgentLoop
from agent.core.model import Message
from agent.core.transport import AgentTransport
from agent.context import SessionMemory, SessionMemoryConfig
from agent.context.tokens import _estimate_tokens
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
        # M5.4 后台 Subagent：{task_id: asyncio.Task}，用于 /bg 查询和会话退出前等待
        self._bg_tasks: dict[str, asyncio.Task] = {}

        # M4.4 Session Memory：零成本优先压缩器（复用 M5.4.1 后台 Subagent 做增量更新）
        # 前置：要求 Auto Compact 开启，否则不启用（见 4.4.2）。
        self.session_id = uuid.uuid4().hex
        self.session_memory: SessionMemory | None = None
        if settings.context.session_memory_enabled and settings.context.auto_compact_enabled:
            self.session_memory = SessionMemory(
                SessionMemoryConfig(
                    session_memory_dir=settings.context.session_memory_dir,
                    minimum_message_tokens_to_init=settings.context.session_memory_min_message_tokens,
                    minimum_tokens_between_update=settings.context.session_memory_min_tokens_between,
                    tool_calls_between_updates=settings.context.session_memory_tool_calls_between,
                    enabled=True,
                ),
                session_id=self.session_id,
            )
        # M4.4 记忆增量更新计数器（串行化：同时只有一个提取在运行）
        self._sm_last_tokens = 0
        self._sm_prev_len = 0
        self._sm_updating = False

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

            # M4.4：本轮结束后检查是否触发后台 Session Memory 增量更新（零成本首选）
            self._maybe_trigger_session_memory(transport)

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
    # M5.4：后台 Subagent 支持
    # ------------------------------------------------------------------ #
    def spawn_background(
        self,
        agent_name: str,
        task: str,
        transport: AgentTransport,
        *,
        parent_span=None,
        result_sink=None,
        on_done=None,
    ) -> str | None:
        """启动一个后台 Subagent，返回 task_id（失败返回 None）。

        后台 Subagent 在独立 asyncio.Task 中运行，完成后：
        - 默认把摘要作为 user 消息注入 ``self.messages`` 并通知用户；
        - 若传入 ``result_sink(agent_name, task, text)``，则改由 sink 消费结果
          （如 M4.4 记忆子 agent：把摘要落盘到 summary.md，而非注入对话）；
        - 若传入 ``on_done(success: bool)``，在任务收尾（成功/失败）时回调，
          用于释放串行化锁等。

        复用 M5.4 后台 Subagent 机制：独立 loop/context/沙箱/权限，主上下文只拿摘要。
        """
        if self.subagent_spawner is None:
            transport.notify("subagents 未启用（settings.subagents.enabled=false）")
            return None
        spec = self.subagent_spawner.get(agent_name)
        if spec is None:
            transport.notify(f"未找到 subagent: {agent_name}")
            return None
        task_id = f"bg_{uuid.uuid4().hex[:8]}"

        async def _run() -> None:
            try:
                result = await self.subagent_spawner.spawn(
                    spec, task,
                    depth=0,
                    parent_span=parent_span or self.root_span,
                    base_registry=self.loop.registry,
                    base_model=self.loop.model,
                    parent_transport=transport,
                    parent_sandbox=self.loop.sandbox,
                    parent_gate=self.loop.gate,
                )
                if result_sink is not None:
                    result_sink(agent_name, task, result.text)
                    transport.notify(
                        f"✅ 后台 Subagent [{agent_name}] 已完成，结果已处理。"
                    )
                else:
                    summary = f"[Background Subagent {agent_name} — {task}]\n{result.text}"
                    self.messages.append(Message(role="user", content=summary))
                    transport.notify(
                        f"✅ 后台 Subagent [{agent_name}] 已完成！摘要已注入会话。"
                    )
            except Exception as e:
                transport.notify(
                    f"❌ 后台 Subagent [{agent_name}] 出错: {type(e).__name__}: {e}"
                )
            finally:
                self._bg_tasks.pop(task_id, None)
                if on_done is not None:
                    on_done(True)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 没有运行中的事件循环（如 CLI 同步上下文），创建一个新循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        bg_task = asyncio.ensure_future(_run(), loop=loop)
        self._bg_tasks[task_id] = bg_task
        return task_id

    def list_background_tasks(self) -> list[dict]:
        """返回运行中的后台任务列表（用于 CLI /bg 展示）。"""
        return [
            {
                "id": tid,
                "agent": t.get_name() if hasattr(t, "get_name") else tid,
                "done": t.done(),
            }
            for tid, t in self._bg_tasks.items()
        ]

    # ------------------------------------------------------------------ #
    # M4.4：Session Memory 增量更新（复用 M5.4.1 后台 Subagent 机制）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _estimate_conv_tokens(msgs: list[Message]) -> int:
        """估算消息列表总 token 数。"""
        text = ""
        for m in msgs:
            text += (m.content or "")
            if m.tool_calls:
                for tc in m.tool_calls:
                    text += tc.name + str(tc.arguments)
        return _estimate_tokens(text)

    def _maybe_trigger_session_memory(self, transport: AgentTransport) -> None:
        """后处理钩子：判断本轮是否该增量更新会话摘要，命中则启动后台记忆子 agent。

        串行化：已有提取在运行时直接跳过（避免并发写 summary.md）。
        不阻塞主对话——记忆子 agent 在后台独立 asyncio.Task 运行，完成落盘到 summary.md。
        """
        sm = self.session_memory
        if sm is None or self._sm_updating:
            return
        conv_tokens = self._estimate_conv_tokens(self.messages)
        tokens_since = conv_tokens - self._sm_last_tokens
        new_msgs = self.messages[self._sm_prev_len:]
        tool_calls = sum(
            1 for m in new_msgs if m.role == "assistant" and m.tool_calls
        )
        last_round_has_tool = any(
            m.role == "assistant" and m.tool_calls for m in reversed(new_msgs)
        )
        if sm.should_update(conv_tokens, tokens_since, tool_calls, last_round_has_tool):
            # 命中：推进计数基线，启动一次后台提取
            self._sm_last_tokens = conv_tokens
            self._sm_prev_len = len(self.messages)
            self._trigger_session_memory_update(transport)

    def _trigger_session_memory_update(self, transport: AgentTransport) -> str | None:
        """启动后台「记忆子 agent」增量更新摘要（复用 spawn_background）。返回 task_id。"""
        assert self.session_memory is not None
        self._sm_updating = True
        task = self.session_memory.build_extraction_task()

        def _sink(agent_name: str, task: str, text: str) -> None:
            if text and self.session_memory is not None:
                self.session_memory.save(
                    text, stats={"source": "background_subagent"}
                )

        return self.spawn_background(
            "session-memory", task, transport,
            parent_span=self.root_span,
            result_sink=_sink,
            on_done=lambda _: setattr(self, "_sm_updating", False),
        )

    def collect_other_session_summaries(self) -> list[tuple[str, str]]:
        """跨会话 Recall（M4.4.6）：收集本项目其它 session 的摘要，供新会话注入参考。

        返回 [(session_id, summary), ...]，按修改时间倒序。当前 session 自身排除在外。
        """
        sm = self.session_memory
        if sm is None:
            return []
        base = sm._summary_path.parent.parent  # <dir>/<session_id>
        out: list[tuple[str, str]] = []
        if not base.is_dir():
            return out
        for d in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir() or d.name == self.session_id:
                continue
            sp = d / "session-memory" / "summary.md"
            if sp.is_file():
                txt = sp.read_text(encoding="utf-8").strip()
                if txt:
                    out.append((d.name, txt))
        return out

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
