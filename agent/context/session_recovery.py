"""会话恢复（M6.2）：EventStream → OpenAI 兼容 messages 重建 + 中断检测。

把持久化的 ``EventStream``（状态单一事实来源）重放为可用于下一轮 ``step`` 的
``list[Message]``，并检测「上轮工具执行中途中断」以便干净续跑（绝不出现悬空
``tool_calls``，呼应 M1.5 的 tool_calls→tool_result 配对铁律）。
"""

from __future__ import annotations

from agent.core.events import EventStream, EventType
from agent.core.model import Message, ToolCall

# 中断后注入的续跑提示：使下一轮 step 重新决策，而非卡在悬空 tool_calls。
INTERRUPTION_PROMPT = "（会话在上次中断处恢复，请继续执行未完成的工具调用）"


def detect_interruption(stream: EventStream) -> bool:
    """检测上轮是否在工具执行中途中断。

    规则：存在 ``TOOL_USE`` 事件但缺少对应 ``TOOL_RESULT``（工具调用已发出、进程却
    在结果落盘前崩溃/被终止）→ 视为中断。与回放缓冲（M7.4）的「仅收非 transient
    事件」一致：瞬时 ``tool_call_delta`` 不参与判定。
    """
    use_ids: set[str] = set()
    result_ids: set[str] = set()
    for ev in stream.all():
        if ev.type == EventType.TOOL_USE and ev.tool_use is not None:
            use_ids.add(ev.tool_use.id)
        elif ev.type == EventType.TOOL_RESULT and ev.tool_call_id is not None:
            result_ids.add(ev.tool_call_id)
    return len(use_ids - result_ids) > 0


def rebuild_messages(
    stream: EventStream,
    *,
    on_interruption: str | None = INTERRUPTION_PROMPT,
) -> list[Message]:
    """把 ``EventStream`` 重放为 OpenAI 兼容 ``Message`` 列表。

    事件→消息映射：
    - ``USER``        → user(content=task)
    - ``DECISION``    → assistant(text + tool_calls)（每轮决策的唯一真相来源）
    - ``TOOL_RESULT`` → tool(content, tool_call_id)
    - ``CLARIFY``     → 为上一轮 pending 的澄清 tool_calls 补一条合成 tool 消息
    - ``PLAN``        → 为上一轮 pending 的 present_plan tool_calls 补合成 tool 消息
    - ``FINAL`` / ``TEXT`` / ``TOOL_USE`` / ``PLAN_PROGRESS`` / ``ERROR`` → 忽略
      （FINAL/TEXT 信息已含于 DECISION；TOOL_USE 与 TOOL_RESULT 配对；其余不进协议）

    中断处理：若末轮存在未配对的 ``tool_calls``（``detect_interruption`` 同类语义），
    丢弃该悬空 assistant 消息，并（默认）注入一条 user 续跑提示，使 messages 始终合法、
    可直接续跑。
    """
    messages: list[Message] = []
    pending: dict[str, ToolCall] = {}  # tool_call_id -> 等待结果的 ToolCall

    for ev in stream.all():
        t = ev.type
        if t == EventType.USER:
            messages.append(Message(role="user", content=ev.text or ""))
        elif t == EventType.DECISION:
            dec = ev.decision
            if dec is None:
                continue
            tcs = list(dec.tool_calls)
            messages.append(Message(role="assistant", content=dec.text, tool_calls=tcs or None))
            for tc in tcs:
                pending[tc.id] = tc
        elif t == EventType.TOOL_RESULT:
            content = ""
            if ev.tool_result is not None:
                content = ev.tool_result.output or ev.tool_result.error or ""
            messages.append(Message(role="tool", content=content, tool_call_id=ev.tool_call_id))
            if ev.tool_call_id is not None:
                pending.pop(ev.tool_call_id, None)
        elif t == EventType.CLARIFY:
            # 上一轮 DECISION 含 ask_clarification tool_calls，补合成结果消息以完成配对
            for tcid in list(pending.keys()):
                messages.append(
                    Message(
                        role="tool",
                        content="已向用户提出澄清问题；用户的回答见随后的 user 消息。",
                        tool_call_id=tcid,
                    )
                )
                pending.pop(tcid, None)
        elif t == EventType.PLAN:
            for tcid in list(pending.keys()):
                messages.append(
                    Message(
                        role="tool",
                        content="计划已提交并落盘，等待用户确认后继续执行。",
                        tool_call_id=tcid,
                    )
                )
                pending.pop(tcid, None)
        # 其余事件类型：忽略（见函数文档）

    # 中断：末轮 tool_calls 未全部配对 → 丢弃悬空 assistant 并（默认）注入续跑提示
    if pending:
        while (
            messages
            and messages[-1].role == "assistant"
            and messages[-1].tool_calls
            and any(tc.id in pending for tc in messages[-1].tool_calls)
        ):
            messages.pop()
        if on_interruption is not None:
            messages.append(Message(role="user", content=on_interruption))

    return messages
