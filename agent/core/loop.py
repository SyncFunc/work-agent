"""ReAct 循环编排（确定性；AI 只做决策）。

设计要点（详见里程碑 1.3）：
- 跑「决策 → 工具 → 观察」循环；同一次 Decision 内的多个 tool_calls 并发执行
  （asyncio.gather + Semaphore(max_tool_concurrency)），轮与轮之间串行。
- 事件流（events.py）作为状态单一事实来源。
- 两层防失控：max_iterations 软上限 + LoopStalled 语义检测。
- 工具侧异常（含 UnknownTool）降级为 ToolResult(ok=False)，不中断循环。
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.config.settings import Settings
from agent.core.control_tools import (
    ASK_CLARIFICATION_TOOL_NAME,
    UPDATE_PLAN_TOOL_NAME,
    PRESENT_PLAN_TOOL_NAME,
    collect_control_tools,
)
from agent.core.events import Event, EventStream
from agent.core.intent import Question, extract_clarify
from agent.core.model import Decision, Message, Model, ToolCall
from agent.core.plan import Plan, PlanStep, PlanStore
from agent.core.transport import AgentTransport
from agent.core.prompts import load_prompt
from agent.obs.tracer import Tracer
from agent.runtime.approval import Action, ApprovalGate
from agent.runtime.registry import ToolRegistry, ToolResult, UnknownTool
from agent.runtime.sandbox import ExecRequest, Executor, SandboxProfile


class LoopMaxIteration(RuntimeError):
    """历史保留，不再抛出。"""


class LoopStalled(RuntimeError):
    """模型原地打转：相邻轮调用签名集合重复达 max_repeat_calls。"""


@dataclass
class AgentResult:
    text: str
    events: EventStream
    iterations: int
    needs_clarification: bool = False
    questions: list[Question] | None = None
    messages: list[Message] | None = None
    clarify_total: int = 0
    plan: str | None = None
    plan_path: str | None = None
    plan_steps: list[PlanStep] | None = None
    needs_plan_confirm: bool = False
    usage: dict[str, int] = field(default_factory=dict)
    soft_limit_hit: bool = False


def _canonical(args: dict) -> str:
    return json.dumps(args, sort_keys=True, ensure_ascii=False)


class AgentLoop:
    def __init__(
        self,
        model: Model,
        registry: ToolRegistry,
        settings: Settings,
        tracer: Tracer | None = None,
        plan_mode: bool | None = None,
        plan_path: str | None = None,
        sandbox: "Executor | None" = None,
        gate: "ApprovalGate | None" = None,
    ) -> None:
        self.model = model
        self.registry = registry
        self.settings = settings
        self.tracer = tracer
        self.plan_mode = plan_mode if plan_mode is not None else settings.plan.mode
        self.plan_path = plan_path
        self.sandbox = sandbox
        self.gate = gate

    async def run(
        self,
        task: str,
        messages: list[Message] | None = None,
        *,
        clarify_total: int = 0,
        plan_mode: bool | None = None,
        plan_path: str | None = None,
        transport: "AgentTransport | None" = None,
    ) -> AgentResult:
        pm = plan_mode if plan_mode is not None else self.plan_mode
        pp = plan_path if plan_path is not None else self.plan_path
        self._run_pm = pm
        self._run_pp = pp
        self._transport = transport

        conv = list(messages) if messages else []
        conv.append(Message(role="user", content=task))
        stream = EventStream()
        if transport is not None:
            transport.bind(stream)
        usage_total: dict[str, int] = {}

        last_callset: frozenset[tuple[str, str]] | None = None
        repeat_count = 0
        ct = clarify_total

        with self._span("agent.run", parent=None) as self._agent_span:
            for i in range(self.settings.loop.max_iterations):
                decision = await self._decide(conv, stream, plan_mode=pm, plan_path=pp)
                if decision.usage:
                    for k, v in decision.usage.items():
                        usage_total[k] = usage_total.get(k, 0) + v
                stream.append(Event(type="decision", decision=decision))

                # ① 澄清闸门
                if self.settings.clarify.enabled and (cq := extract_clarify(decision)) is not None:
                    ct += 1
                    if self._agent_span is not None:
                        self._agent_span.log("clarify", f"round {ct}: {len(cq)} questions")
                    stream.append(Event(type="clarify", questions=[q.to_dict() for q in cq]))
                    if ct <= self.settings.clarify.max_rounds:
                        clarify_calls = [
                            tc for tc in decision.tool_calls
                            if tc.name == ASK_CLARIFICATION_TOOL_NAME
                        ]
                        conv.append(Message(role="assistant", tool_calls=clarify_calls))
                        for tc in clarify_calls:
                            conv.append(Message(
                                role="tool", tool_call_id=tc.id,
                                content="已向用户提出澄清问题；用户的回答见随后的 user 消息。",
                            ))
                        return AgentResult(
                            text="", events=stream, iterations=i + 1,
                            needs_clarification=True, questions=cq,
                            messages=conv, clarify_total=ct, usage=usage_total,
                        )

                # ② 计划闸门
                if pm and (ppp := self._find_tool_args(decision, PRESENT_PLAN_TOOL_NAME)) is not None:
                    plan = Plan(
                        body=ppp.get("body", ""),
                        steps=[PlanStep(id=s["id"], title=s.get("title", ""), status="pending")
                               for s in ppp.get("steps", [])],
                    )
                    path = PlanStore.write_plan(plan, self.settings.plan.file)
                    stream.append(Event(type="plan", text=plan.body, plan_path=path))
                    present_calls = [tc for tc in decision.tool_calls if tc.name == PRESENT_PLAN_TOOL_NAME]
                    conv.append(Message(role="assistant", tool_calls=present_calls))
                    for tc in present_calls:
                        conv.append(Message(
                            role="tool", tool_call_id=tc.id,
                            content="计划已提交并落盘，等待用户确认后继续执行。",
                        ))
                    return AgentResult(
                        text="", events=stream, iterations=i + 1,
                        plan=plan.body, plan_path=path, plan_steps=plan.steps,
                        needs_plan_confirm=True, messages=conv,
                        clarify_total=ct, usage=usage_total,
                    )

                if decision.is_final:
                    text = decision.text or ""
                    stream.append(Event(type="final", text=text))
                    conv.append(Message(role="assistant", content=text))
                    return AgentResult(
                        text=text, events=stream, iterations=i + 1,
                        messages=conv, clarify_total=ct, usage=usage_total,
                    )

                results = await self._exec_tools(decision.tool_calls, stream)

                callset = frozenset(
                    (tc.name, _canonical(tc.arguments)) for tc in decision.tool_calls
                )
                if callset == last_callset:
                    repeat_count += 1
                else:
                    repeat_count = 0
                last_callset = callset
                if repeat_count >= self.settings.loop.max_repeat_calls:
                    if self._agent_span is not None:
                        self._agent_span.log("stall", f"repeated {repeat_count + 1} times: {sorted(callset)}", level="error")
                    raise LoopStalled(
                        f"model repeated identical tool calls {repeat_count + 1} times; "
                        f"possible infinite loop on {sorted(callset)}"
                    )

                conv.append(Message(role="assistant", tool_calls=decision.tool_calls))
                for tc, res in zip(decision.tool_calls, results):
                    stream.append(Event(type="tool_result", tool_call_id=tc.id, tool_result=res))
                    conv.append(
                        Message(role="tool", content=res.output or res.error, tool_call_id=tc.id)
                    )

        notice = (
            f"⚠️ 已到达最大轮次上限（{self.settings.loop.max_iterations}），本轮未产出最终答案。"
            "上下文已保留，可继续输入指令（如「继续」）在现有基础上接棒执行。"
        )
        if self._agent_span is not None:
            self._agent_span.log("soft_limit", notice, level="warn")
        stream.append(Event(type="final", text=notice))
        return AgentResult(
            text=notice, events=stream, iterations=self.settings.loop.max_iterations,
            messages=conv, clarify_total=ct, usage=usage_total, soft_limit_hit=True,
        )

    async def _decide(
        self, conv: list[Message], stream: EventStream, *,
        plan_mode: bool = False, plan_path: str | None = None,
    ) -> Decision:
        full = [Message(role="system", content=self._system_prompt(plan_mode=plan_mode, plan_path=plan_path))] + conv
        decision: Decision | None = None
        with self._span("model.act", kind="model", parent=self._agent_span) as mspan:
            if mspan is not None:
                mspan.log("conv_len", len(conv))
                mspan.log("plan_mode", plan_mode)
            async for ev in self.model.stream(full, tools=self._model_tools(plan_mode=plan_mode, plan_path=plan_path)):
                if ev.type == "text" and ev.text:
                    stream.append(Event(type="text", text=ev.text, kind=ev.kind or "content"))
                elif ev.type == "tool_call_delta":
                    stream.emit(Event(
                        type="tool_call_delta", tc_index=ev.tc_index,
                        tc_name=ev.tc_name, tc_args=ev.tc_args,
                    ))
                elif ev.type == "done":
                    decision = ev.decision
                    if mspan is not None and decision is not None and decision.usage:
                        mspan.meta["usage"] = decision.usage
        if decision is None:
            decision = Decision(text="")
            if mspan is not None:
                mspan.log("decision_empty", True, level="warn")
        else:
            decision.tool_calls = [tc for tc in decision.tool_calls if tc.name and tc.name.strip()]
            if mspan is not None:
                mspan.log("tool_calls", len(decision.tool_calls))
                if decision.is_final and decision.text:
                    mspan.log("final_text_len", len(decision.text))
        return decision

    def _model_tools(self, *, plan_mode: bool = False, plan_path: str | None = None) -> list[dict]:
        registry_tools = [spec.to_openai() for spec in self.registry.list()]
        control = collect_control_tools(self.settings.clarify, plan_mode=plan_mode, has_plan=bool(plan_path))
        return registry_tools + control

    def _system_prompt(self, *, plan_mode: bool = False, plan_path: str | None = None) -> str:
        prompt = load_prompt("system")
        try:
            _net_allowed = SandboxProfile(self.settings.sandbox.profile) == SandboxProfile.DANGER_FULL
        except ValueError:
            _net_allowed = False
        return prompt.render(
            clarify_enabled=self.settings.clarify.enabled,
            plan_mode=plan_mode,
            has_plan=bool(plan_path),
            sandbox_profile=self.settings.sandbox.profile,
            approval_mode=self.settings.approval.mode,
            network_allowed=_net_allowed,
            sandbox_exec_policy=list(self.settings.approval.exec_policy),
        )

    @contextmanager
    def _span(self, name: str, *, kind: str = "span", parent: Any | None = None):
        if self.tracer is None:
            yield None
        else:
            with self.tracer.span(name, kind=kind, parent=parent) as s:
                yield s

    @staticmethod
    def _find_tool_args(decision: Decision, name: str) -> dict[str, Any] | None:
        for tc in decision.tool_calls:
            if tc.name == name:
                return tc.arguments
        return None

    async def _exec_tools(
        self, calls: list[ToolCall], stream: EventStream
    ) -> list[ToolResult]:
        if not calls:
            return []
        sem = asyncio.Semaphore(self.settings.loop.max_tool_concurrency)

        async def _one(tc: ToolCall) -> ToolResult:
            with self._span("tool.exec", kind="tool", parent=self._agent_span) as tool_span:
                if tool_span is not None:
                    tool_span.log("tool", tc.name)
                    tool_span.log("args", json.dumps(tc.arguments, ensure_ascii=False)[:200])
                # 控制/虚拟工具
                if tc.name == UPDATE_PLAN_TOOL_NAME:
                    a = tc.arguments
                    step_id = a.get("step_id")
                    status = a.get("status", "done")
                    note = a.get("note")
                    if not step_id:
                        return ToolResult(ok=False, error="update_plan requires step_id")
                    try:
                        PlanStore.update_step(
                            self._run_pp or self.settings.plan.file, step_id, status, note
                        )
                    except (KeyError, ValueError) as e:
                        return ToolResult(ok=False, error=str(e))
                    stream.append(Event(
                        type="plan_progress",
                        plan_path=self._run_pp or self.settings.plan.file,
                        plan_update={"step_id": step_id, "status": status, "note": note},
                    ))
                    return ToolResult(ok=True, output="progress updated")

                # 未知工具
                try:
                    spec = self.registry.get(tc.name)
                except UnknownTool:
                    if tool_span is not None:
                        tool_span.log("unknown_tool", tc.name, level="warn")
                    return ToolResult(ok=False, error=f"unknown tool: {tc.name}")

                # 审批门：执行前过 ApprovalGate
                elevated: SandboxProfile | None = None
                if self.gate is not None:
                    action = Action(
                        tool=tc.name,
                        risk=spec.risk,
                        args=tc.arguments or {},
                        description=self._describe(tc),
                        approval_request=tc.approval_request,
                    )
                    d = self.gate.decide(action)
                    if d.verdict == "ask":
                        if tool_span is not None:
                            tool_span.log("approval_ask", d.reason)
                        ok = await self.gate.authorize(action, self._transport)
                        if not ok:
                            if tool_span is not None:
                                tool_span.log("approval_rejected", True, level="warn")
                            return ToolResult(ok=False, error="rejected by user approval")
                        # 批准后自动提权（若命令需联网且当前 profile 不允许）
                        elevated = d.elevated_profile
                    # verdict == "allow"：elevated 保持 None（不提权）

                # 执行
                async with sem:
                    try:
                        if tc.name == "bash" and self.sandbox is not None:
                            return await self._run_bash_in_sandbox(tc.arguments or {}, elevated)
                        return await self.registry.run(
                            tc.name, tc.arguments, self.settings.loop.max_tool_output_chars
                        )
                    except Exception as e:
                        if tool_span is not None:
                            tool_span.log("exec_error", f"{type(e).__name__}: {e}", level="error")
                        return ToolResult(ok=False, error=f"{type(e).__name__}: {e}")

        for tc in calls:
            stream.append(Event(type="tool_use", tool_use=tc))
        results = list(await asyncio.gather(*(_one(tc) for tc in calls)))
        return results

    async def _run_bash_in_sandbox(
        self, args: dict[str, Any], elevated_profile: "SandboxProfile | None"
    ) -> ToolResult:
        sandbox = self.sandbox
        assert sandbox is not None, "caller must ensure sandbox is set before calling"
        cmd = args.get("cmd", "")
        timeout = args.get("timeout", 30)
        env = dict(os.environ)
        env["LANG"] = "C.UTF-8"
        env["LC_ALL"] = "C.UTF-8"
        profile = elevated_profile if elevated_profile is not None else sandbox.default_profile
        req = ExecRequest(cmd=cmd, cwd=Path.cwd(), env=env, timeout=timeout, profile=profile)
        r = await sandbox.run(req)
        return ToolResult(ok=r.ok, output=r.output, error=r.error)

    @staticmethod
    def _describe(tc: ToolCall) -> str:
        a = tc.arguments or {}
        if "cmd" in a:
            return f"bash: {a['cmd']}"
        if "path" in a:
            return f"{tc.name}: {a['path']}"
        return f"{tc.name}: {a}"
