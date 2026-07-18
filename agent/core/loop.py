"""ReAct 循环编排（确定性；AI 只做决策）。

设计要点（详见里程碑 1.3）：
- 跑「决策 → 工具 → 观察」循环；同一次 Decision 内的多个 tool_calls 并发执行
  （asyncio.gather + Semaphore(max_tool_concurrency)），轮与轮之间串行。
- 事件流（events.py）作为状态单一事实来源，每个决策/工具调用/结果都落成事件。
- 两层防失控：max_iterations 软上限（触顶不中断，返回提示并把累计上下文交还会话层接棒）
  + LoopStalled 语义检测（重复调用/卡死，仍作硬中断，因其表示模型原地打转需人工介入）。
- 工具侧异常（含 UnknownTool）降级为 ToolResult(ok=False)，不中断循环，让模型自纠。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
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
from agent.core.presenter import LoopPresenter
from agent.core.plan import Plan, PlanStep, PlanStore
from agent.core.prompts import load_prompt
from agent.obs.tracer import Tracer
from agent.runtime.registry import RISK_LEVELS, ToolRegistry, ToolResult, UnknownTool
from agent.tools.bash import is_readonly_command


class LoopMaxIteration(RuntimeError):
    """（历史）超过 max_iterations 的兜底异常。

    M1.x 起已不再抛出：触顶改为软返回（见 ``run`` 末段）——返回带提示的 ``AgentResult``
    并把累计对话 context 交还会话层，使 chat REPL 可自然进入下一轮、用户接棒续跑，
    避免异常中断导致历史丢失、被迫从头重规划。本类保留仅为向后兼容（导出命名空间）。
    """


class LoopStalled(RuntimeError):
    """模型原地打转：相邻轮调用签名集合重复达 max_repeat_calls。"""


@dataclass
class AgentResult:
    text: str
    events: EventStream
    iterations: int
    needs_clarification: bool = False       # M1.5：模型在澄清闸门提前返回
    questions: list[Question] | None = None  # M1.5：待用户回答的问题清单
    # 本次会话更新后的对话历史（仅 user/assistant/tool，不含 system）。
    # loop 无状态、不持有对话：会话层负责持有并在多次 run 间传入/持久化。
    messages: list[Message] | None = None
    # 跨澄清轮次的累计计数（会话级护栏，需会话层在多次 run 间传入/回传）。
    clarify_total: int = 0
    # PLAN 模式（M1.4）：present_plan 落盘后提前返回，供 CLI 确认。
    plan: str | None = None
    plan_path: str | None = None
    plan_steps: list[PlanStep] | None = None
    needs_plan_confirm: bool = False
    # 本轮 ReAct 循环累计的 token 用量（逐次 model 调用的 usage 累加）。
    usage: dict[str, int] = field(default_factory=dict)
    # 软上限命中标记（M1.x 修复）：max_iterations 触顶时不再抛异常中断，而是返回
    # 一条「已达最大轮次」的提示结果，并把累计对话 context 一并返回，使会话可续。
    # 上层 chat REPL 据此自然进入下一轮（用户说「继续」即可在现有上下文基础上接棒，
    # 不会因异常丢失历史而被迫从头重规划）。
    soft_limit_hit: bool = False


def _canonical(args: dict) -> str:
    """参数规范化：sort_keys 使参数顺序无关，避免「换顺序伪装成新调用」。"""
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
    ) -> None:
        self.model = model
        self.registry = registry
        self.settings = settings
        self.tracer = tracer  # 预留：M1.4 韧性 / M5 trace 接入
        # plan_mode 缺省取 settings.plan_mode（M1.4 设计：CLI 可显式覆盖）
        self.plan_mode = plan_mode if plan_mode is not None else settings.plan_mode
        self.plan_path = plan_path          # M1.4：执行期已知计划文件路径（触发 update_plan 控制工具）
        # 注意：loop 不持有任何对话状态（messages 由会话层持有并在 run 间传入/回传），
        # 见 run() 的 messages / clarify_total 参数与 AgentResult.messages / clarify_total 回传。

    async def run(
        self,
        task: str,
        messages: list[Message] | None = None,
        *,
        clarify_total: int = 0,
        plan_mode: bool | None = None,
        plan_path: str | None = None,
        presenter: "LoopPresenter | None" = None,
    ) -> AgentResult:
        """执行一次 ReAct 循环。

        - ``messages``：本次会话已有的对话历史（仅 user/assistant/tool，**不含 system**），
          由会话层持有。为 ``None`` 时视为空会话。loop 不会保存它，跨 run 的连续性
          完全由会话层决定（传入旧历史、再用 ``AgentResult.messages`` 更新）。
        - ``clarify_total``：跨澄清轮次的累计计数（会话级护栏），由会话层在多次 run 间传入。
        - ``plan_mode`` / ``plan_path``：**本次 run 的模式**（plan 探索 / exec 执行）。
          为 ``None`` 时回落构造期的 ``self.plan_mode`` / ``self.plan_path`` 缺省值。
          会话层可在任意轮次切换——传入不同值即可，loop 不保存任何模式状态，
          因此「plan / exec 自由切换」由会话层（CLI）持有并在每次 run 间传入。
        - 返回 ``AgentResult.messages``：本轮更新后的对话历史；``AgentResult.clarify_total``：更新后的计数。
        - 澄清回填即「会话层用答案作为新 task、带上旧 messages 再次 run」。
        """
        # 本次 run 的模式：显式覆盖优先，否则用构造期缺省（会话层可在任意轮次切换）。
        pm = plan_mode if plan_mode is not None else self.plan_mode
        pp = plan_path if plan_path is not None else self.plan_path
        # 供 _exec_tools 在本轮内读取（本轮模式，跨 run 不持久）。
        self._run_pm = pm
        self._run_pp = pp

        # 本轮对话在本函数内局部累积（含本轮 user 任务），不写入任何实例字段。
        conv = list(messages) if messages else []
        conv.append(Message(role="user", content=task))
        stream = EventStream()
        usage_total: dict[str, int] = {}  # 逐次 model 调用的 token 用量累加

        last_callset: frozenset[tuple[str, str]] | None = None
        repeat_count = 0
        ct = clarify_total

        # 包裹 agent.run span（可观测，M1.6）：tool.exec / model.act 作为其子 span。
        with self._span("agent.run", parent=None) as self._agent_span:
            for i in range(self.settings.max_iterations):
                decision = await self._decide(
                    conv, stream, plan_mode=pm, plan_path=pp, presenter=presenter
                )
                # 一轮模型决策结束：通知 presenter 收尾（如工具参数流式预览 Live）。
                # 关键：澄清/计划闸门会在此轮提前返回、on_tool_call 永不触发，若不在此
                # 收尾，残留 Live 会扰乱随后的澄清面板渲染（重复面板、选项不显示）。
                on_done = getattr(presenter, "on_decision_done", None)
                if on_done is not None:
                    on_done()
                if decision.usage:
                    for k, v in decision.usage.items():
                        usage_total[k] = usage_total.get(k, 0) + v
                stream.append(Event(type="decision", decision=decision))

                # ① 澄清闸门（M1.5，最前）：模糊任务先问后做，澄清前不执行任何工具
                if self.settings.clarify_enabled and (cq := extract_clarify(decision)) is not None:
                    ct += 1
                    stream.append(Event(type="clarify", questions=[q.to_dict() for q in cq]))
                    if ct <= self.settings.max_clarify_rounds:
                        # 记录澄清动作，保留上下文供会话层回填答案后续跑。
                        # 只保留 ask_clarification 本次调用（同轮其它调用按 extract_clarify
                        # 约定忽略），并**必须**为其补一条 tool 回执：否则「assistant(tool_calls)
                        # 之后缺对应 tool 消息」会让下一轮请求触发 400（OpenAI/DeepSeek 协议要求
                        # tool_calls 后紧跟每个 tool_call_id 的 tool 回执）。用户答案随后作为新
                        # user 消息续跑，模型据此继续。
                        clarify_calls = [
                            tc for tc in decision.tool_calls
                            if tc.name == ASK_CLARIFICATION_TOOL_NAME
                        ]
                        conv.append(Message(role="assistant", tool_calls=clarify_calls))
                        for tc in clarify_calls:
                            conv.append(Message(
                                role="tool",
                                tool_call_id=tc.id,
                                content="已向用户提出澄清问题；用户的回答见随后的 user 消息。",
                            ))
                        return AgentResult(
                            text="",
                            events=stream,
                            iterations=i + 1,
                            needs_clarification=True,
                            questions=cq,
                            messages=conv,
                            clarify_total=ct,
                            usage=usage_total,
                        )
                    # 超出上限：不再提前返回，落入执行分支；
                    # ask_clarification 作为未知工具被降级，迫使模型继续或最终 final（防死循环）。

                # ② 计划闸门（M1.4，仅 plan 模式）：present_plan → 落盘 + 提前返回
                if pm and (ppp := self._find_tool_args(decision, PRESENT_PLAN_TOOL_NAME)) is not None:
                    plan = Plan(
                        body=ppp.get("body", ""),
                        steps=[
                            PlanStep(id=s["id"], title=s.get("title", ""), status="pending")
                            for s in ppp.get("steps", [])
                        ],
                    )
                    path = PlanStore.write_plan(plan, self.settings.plan_file)
                    stream.append(Event(type="plan", text=plan.body, plan_path=path))
                    # 同澄清闸门：只保留 present_plan 调用并补 tool 回执，避免用户确认后
                    # 续跑时「assistant(tool_calls) 后缺 tool 消息」触发 400。
                    present_calls = [
                        tc for tc in decision.tool_calls
                        if tc.name == PRESENT_PLAN_TOOL_NAME
                    ]
                    conv.append(Message(role="assistant", tool_calls=present_calls))
                    for tc in present_calls:
                        conv.append(Message(
                            role="tool",
                            tool_call_id=tc.id,
                            content="计划已提交并落盘，等待用户确认后继续执行。",
                        ))
                    return AgentResult(
                        text="",
                        events=stream,
                        iterations=i + 1,
                        plan=plan.body,
                        plan_path=path,
                        plan_steps=plan.steps,
                        needs_plan_confirm=True,
                        messages=conv,
                        clarify_total=ct,
                        usage=usage_total,
                    )

                if decision.is_final:
                    text = decision.text or ""
                    stream.append(Event(type="final", text=text))
                    conv.append(Message(role="assistant", content=text))
                    return AgentResult(
                        text=text, events=stream, iterations=i + 1,
                        messages=conv, clarify_total=ct, usage=usage_total,
                    )

                results = await self._exec_tools(decision.tool_calls, stream, presenter)

                # 卡死检测：本轮调用签名集合 vs 上一轮
                callset = frozenset(
                    (tc.name, _canonical(tc.arguments)) for tc in decision.tool_calls
                )
                if callset == last_callset:
                    repeat_count += 1
                else:
                    repeat_count = 0
                last_callset = callset
                if repeat_count >= self.settings.max_repeat_calls:
                    raise LoopStalled(
                        f"model repeated identical tool calls {repeat_count + 1} times; "
                        f"possible infinite loop on {sorted(callset)}"
                    )

                # 回填本轮对话（仅本函数局部 conv，跨 run 由会话层续接）
                conv.append(Message(role="assistant", tool_calls=decision.tool_calls))
                for tc, res in zip(decision.tool_calls, results):
                    stream.append(Event(type="tool_result", tool_call_id=tc.id, tool_result=res))
                    conv.append(
                        Message(role="tool", content=res.output or res.error, tool_call_id=tc.id)
                    )

        # 触顶软处理（M1.x 修复）：不再抛 LoopMaxIteration 中断会话，而是返回一条
        # 「已达最大轮次」的提示作为本轮结果，并把累计 conv 一并返回，使会话可续
        # （上层 chat REPL 自然进入下一轮；用户说「继续」即可在现有上下文基础上接棒，
        # 不必因异常丢失 N 轮历史而从头重规划）。conv 在此处已含本轮全部对话（含最后
        # 一轮的 assistant(tool_calls) 与对应 tool 回执），上下文自洽、可直接作为下一轮输入。
        notice = (
            f"⚠️ 已到达最大轮次上限（{self.settings.max_iterations}），本轮未产出最终答案。"
            "上下文已保留，可继续输入指令（如「继续」）在现有基础上接棒执行。"
        )
        stream.append(Event(type="final", text=notice))
        return AgentResult(
            text=notice,
            events=stream,
            iterations=self.settings.max_iterations,
            messages=conv,
            clarify_total=ct,
            usage=usage_total,
            soft_limit_hit=True,
        )

    async def _decide(
        self,
        conv: list[Message],
        stream: EventStream,
        *,
        plan_mode: bool = False,
        plan_path: str | None = None,
        presenter: "LoopPresenter | None" = None,
    ) -> Decision:
        """调用模型的**流式**接口，逐片回传文本事件，收尾返回完整 Decision。

        ``conv`` 为本次会话对话历史（不含 system）；system 提示由 loop 临时拼接到
        模型调用前，不写入会话历史。整体包在 ``model.act`` span 下（可观测）。
        ``plan_mode`` / ``plan_path`` 为本轮模式（支持任意轮次切换）。
        ``presenter`` 非空时，逐片文本（区分 reasoning/content）实时回调渲染。
        """
        full = [Message(role="system", content=self._system_prompt(plan_mode=plan_mode, plan_path=plan_path))] + conv
        decision: Decision | None = None
        on_delta = getattr(presenter, "on_tool_call_delta", None) if presenter is not None else None
        with self._span("model.act", kind="model", parent=self._agent_span) as mspan:
            async for ev in self.model.stream(full, tools=self._model_tools(plan_mode=plan_mode, plan_path=plan_path)):
                if ev.type == "text" and ev.text:
                    kind = ev.kind or "content"
                    stream.append(Event(type="text", text=ev.text, kind=kind))  # 实时 token，供 UI/可观测
                    if presenter is not None:
                        presenter.on_text(ev.text, kind)
                elif ev.type == "tool_call_delta" and on_delta is not None:
                    # 工具调用参数流式预览（write/edit 的内容在生成过程中即显示）。
                    # 用增量事件里的累计信息，presenter 自行决定如何渲染，loop 不解析参数。
                    on_delta(ev.tc_index, ev.tc_name, ev.tc_args)
                elif ev.type == "done":
                    decision = ev.decision
                    # 模型调用 span 记录完整 usage 结构（供可观测 / 后续导出 Langfuse 等）
                    if mspan is not None and decision is not None and decision.usage:
                        mspan.meta["usage"] = decision.usage
        if decision is None:
            decision = Decision(text="")
        else:
            # 清理流式协议边界噪声：DeepSeek/OpenAI 在「带 tools 的纯文本回复」时，偶尔会在
            # 流式末尾附带 name 为空的 tool_call。若不丢弃，decision.tool_calls 非空 →
            # is_final=False → 落入执行分支（空 name 被当 UnknownTool 降级），模型下一轮又
            # 输出相同文本，造成「纯文本刷屏」死循环。过滤后纯文本回复的 tool_calls 为空
            # → is_final=True → 直接作为 final 返回，循环正常终止。
            decision.tool_calls = [tc for tc in decision.tool_calls if tc.name and tc.name.strip()]
        return decision

    def _model_tools(self, *, plan_mode: bool = False, plan_path: str | None = None) -> list[dict]:
        """合并注册表真实工具 + 控制工具（按模式，由 control_tools 集中管理）。"""
        registry_tools = [spec.to_openai() for spec in self.registry.list()]
        control = collect_control_tools(
            self.settings,
            plan_mode=plan_mode,
            has_plan=bool(plan_path),
        )
        return registry_tools + control

    def _system_prompt(self, *, plan_mode: bool = False, plan_path: str | None = None) -> str:
        # 系统提示外置为结构化文件 agent/prompts/system.md（frontmatter + Jinja2 模板）。
        prompt = load_prompt("system")
        return prompt.render(
            clarify_enabled=self.settings.clarify_enabled,
            plan_mode=plan_mode,
            has_plan=bool(plan_path),
        )

    @contextmanager
    def _span(self, name: str, *, kind: str = "span", parent: Any | None = None):
        """统一 span 入口：tracer 为 None 时降级为无操作（不破坏无观测场景）。"""
        if self.tracer is None:
            yield None
        else:
            with self.tracer.span(name, kind=kind, parent=parent) as s:
                yield s

    @staticmethod
    def _find_tool_args(decision: Decision, name: str) -> dict[str, Any] | None:
        """在决策的工具调用里找指定名的参数；不存在返回 None。"""
        for tc in decision.tool_calls:
            if tc.name == name:
                return tc.arguments
        return None

    def _risk_blocked(self, risk: str) -> bool:
        """plan 模式下：风险高于阈值则拦截（默认阈值 'read'，仅放行 read）。未知风险保守拦截。"""
        order = RISK_LEVELS
        threshold = self.settings.plan_mode_block_risk_above
        try:
            return order.index(risk) > order.index(threshold)
        except ValueError:
            return True

    async def _exec_tools(
        self, calls: list[ToolCall], stream: EventStream, presenter: "LoopPresenter | None" = None
    ) -> list[ToolResult]:
        if not calls:
            return []
        sem = asyncio.Semaphore(self.settings.max_tool_concurrency)

        async def _one(tc: ToolCall) -> ToolResult:
            with self._span("tool.exec", kind="tool", parent=self._agent_span):
                # 控制/虚拟工具：update_plan（不进 registry，循环内处理写文件 + 事件）
                if tc.name == UPDATE_PLAN_TOOL_NAME:
                    a = tc.arguments
                    step_id = a.get("step_id")
                    status = a.get("status", "done")
                    note = a.get("note")
                    if not step_id:
                        return ToolResult(ok=False, error="update_plan requires step_id")
                    try:
                        updated = PlanStore.update_step(
                            self._run_pp or self.settings.plan_file, step_id, status, note
                        )
                    except (KeyError, ValueError) as e:
                        return ToolResult(ok=False, error=str(e))
                    stream.append(
                        Event(
                            type="plan_progress",
                            plan_path=self._run_pp or self.settings.plan_file,
                            plan_update={"step_id": step_id, "status": status, "note": note},
                        )
                    )
                    # 进度可视化：把更新后的完整 Plan 推给 presenter 渲染步骤列表
                    # （on_plan_progress 为可选协议方法，未实现则静默跳过）。
                    hook = getattr(presenter, "on_plan_progress", None)
                    if hook is not None:
                        hook(updated)
                    return ToolResult(ok=True, output="progress updated")

                # 未知工具 / 计划模式风险门控（确定性兜底，不依赖 prompt 软约束）
                try:
                    spec = self.registry.get(tc.name)
                except UnknownTool:
                    return ToolResult(ok=False, error=f"unknown tool: {tc.name}")
                if self._run_pm and self._risk_blocked(spec.risk):
                    # 默认拦截（确定性兜底）；bash 的只读命令白名单例外（PLAN 模式仍允许探索）。
                    blocked = True
                    if spec.name == "bash":
                        cmd = (tc.arguments or {}).get("cmd", "")
                        if is_readonly_command(cmd, self.settings.plan_mode_bash_allow):
                            blocked = False
                    if blocked:
                        if spec.name == "bash":
                            return ToolResult(
                                ok=False,
                                error=f"plan mode blocks mutating bash: {(tc.arguments or {}).get('cmd', '')!r}",
                            )
                        return ToolResult(
                            ok=False,
                            error=f"plan mode blocks mutating tool: {tc.name} (risk={spec.risk})",
                        )
                async with sem:
                    try:
                        return await self.registry.run(
                            tc.name, tc.arguments, self.settings.max_tool_output_chars
                        )
                    except Exception as e:  # 工具内部未捕获异常同样降级
                        return ToolResult(ok=False, error=f"{type(e).__name__}: {e}")

        # 顺序即时展示调用（执行前），再并发执行，最后顺序展示结果（保序、清晰区分工具调用）。
        # 注意：tool_result *事件* 由 run 的回填循环统一 append（与 conv 回填配对），
        # 此处仅触发 presenter 回调，避免事件重复。
        for tc in calls:
            stream.append(Event(type="tool_use", tool_use=tc))
            if presenter is not None:
                presenter.on_tool_call(tc)
        results = list(await asyncio.gather(*(_one(tc) for tc in calls)))
        for tc, res in zip(calls, results):
            if presenter is not None:
                presenter.on_tool_result(tc, res)
        return results
