"""M7 服务端 BridgeTransport：实现 ``AgentTransport``，事件转发 + HITL 经协议闭环。

``BridgeTransport`` 是 ``AgentTransport`` 的**序列化映射**（不是新协议）：
- 每个 HITL 方法发送一条带 ``id`` 的协议请求，并 ``await`` 一个 ``asyncio.Future``；
  收到客户端回传的同 ``id`` 应答后 ``set_result`` 唤醒 ``Session.step`` 协程（零阻塞跨进程）。
- ``bind(stream)`` 订阅 ``EventStream``，事件按条经 ``event`` 消息实时转发；
  同时写入 ``SessionHandle.event_buffer``（**仅非 ``transient`` 事件**，见修复点②）。
- ``interactive`` 返回 ``True``，使 ``Session.step`` 正常走 ask / confirm_plan / approve 分支。

``Future`` 存于 ``pending: dict[id, Future]``；``resolve(id, value)`` 由 server 在收到客户端
应答时调用。请求 ``id`` 由 daemon 事件循环生成、跨连接唯一。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from agent.core.events import Event
from agent.core.intent import Question
from agent.core.transport import AgentTransport
from agent.runtime.approval import Action

from agent.daemon.protocol import MsgType

if TYPE_CHECKING:
    from agent.core.events import EventStream
    from agent.core.loop import AgentResult
    from agent.core.plan import PlanStep


def _action_to_dict(a: Action) -> dict[str, Any]:
    return {
        "tool": a.tool,
        "risk": a.risk,
        "args": a.args,
        "description": a.description,
        "approval_request": a.approval_request,
    }


def _plan_step_to_dict(s: "PlanStep") -> dict[str, Any]:
    return asdict(s)


def _spec_to_dict(s: object) -> dict[str, Any]:
    if hasattr(s, "to_dict"):
        return s.to_dict()  # type: ignore[attr-defined]
    return {k: v for k, v in vars(s).items() if not k.startswith("_")}


class BridgeTransport(AgentTransport):
    """服务端 AgentTransport 实现：事件转发 + HITL future 闭环。"""

    def __init__(self, handle: "SessionHandle") -> None:
        self.handle = handle
        self._pending: dict[str, asyncio.Future] = {}
        self._pending_sends: list[asyncio.Task] = []  # 在飞的事件转发任务（flush 用）

    def _track(self, task: asyncio.Task) -> None:
        # 修剪已完成任务，避免后台会话无 flush 时列表无限增长。
        self._pending_sends = [t for t in self._pending_sends if not t.done()]
        self._pending_sends.append(task)

    async def flush(self) -> None:
        """等待所有已在飞的事件转发任务完成（保证 FINAL 等事件先于 CLOSE 落地）。"""
        if self._pending_sends:
            await asyncio.gather(*self._pending_sends, return_exceptions=True)
            self._pending_sends = []

    @property
    def interactive(self) -> bool:
        # HITL 由远端客户端承接
        return True

    # ------------------------------------------------------------------ #
    # 发送辅助
    # ------------------------------------------------------------------ #
    def _send(self, mtype: MsgType, payload: dict[str, Any], *, id: str | None = None) -> None:
        conn = self.handle.attached_conn
        if conn is None:
            return
        self._track(
            asyncio.ensure_future(
                conn.send(mtype, payload, id=id, session=self.handle.session_id)
            )
        )

    def resolve(self, rid: str, value: object) -> None:
        """server 收到客户端应答时调用，唤醒等待中的 ``ask`` / ``confirm_plan`` / ``approve``。"""
        fut = self._pending.pop(rid, None)
        if fut is not None and not fut.done():
            fut.set_result(value)

    # ------------------------------------------------------------------ #
    # HITL：请求 -> Future -> 应答唤醒
    # ------------------------------------------------------------------ #
    async def ask(self, question: Question) -> str:
        rid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._send(MsgType.ASK, {"id": rid, "question": question.to_dict()}, id=rid)
        return await fut

    def show_questions(self, questions: list[Question]) -> None:
        self._send(MsgType.SHOW_QUESTIONS, {"questions": [q.to_dict() for q in questions]})

    def show_plan(self, res: "AgentResult") -> None:
        self._send(
            MsgType.SHOW_PLAN,
            {
                "plan": res.plan,
                "plan_path": res.plan_path,
                "plan_steps": [_plan_step_to_dict(s) for s in (res.plan_steps or [])],
            },
        )

    async def confirm_plan(self) -> bool:
        rid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._send(MsgType.CONFIRM_PLAN, {"id": rid}, id=rid)
        return await fut

    async def approve(self, action: Action) -> bool:
        rid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._send(MsgType.APPROVE, {"id": rid, "action": _action_to_dict(action)}, id=rid)
        return await fut

    def notify(self, message: str) -> None:
        self._send(MsgType.NOTIFY, {"message": message})

    def show_skills(self, specs: list) -> None:
        self._send(MsgType.SHOW_SKILLS, {"specs": [_spec_to_dict(s) for s in specs]})

    def show_agents(self, specs: list) -> None:
        self._send(MsgType.SHOW_AGENTS, {"specs": [_spec_to_dict(s) for s in specs]})

    # ------------------------------------------------------------------ #
    # 事件订阅：实时转发 + 持久化缓冲（仅非 transient）
    # ------------------------------------------------------------------ #
    def bind(self, stream: "EventStream") -> None:
        stream.subscribe(self._on_event)

    def _on_event(self, ev: Event) -> None:
        # 修复点②：仅非 transient 事件进回放缓冲（tool_call_delta 等瞬时事件只实时转发）。
        if not ev.transient:
            self.handle.event_buffer.append(ev)
        conn = self.handle.attached_conn
        if conn is not None:
            self._track(
                asyncio.ensure_future(
                    conn.send(MsgType.EVENT, {"event": ev.to_dict()}, session=self.handle.session_id)
                )
            )

    def close(self) -> None:
        self._send(MsgType.CLOSE, {})

    def report_usage(self, usage: dict[str, int] | None, answer: str | None = None) -> None:
        if not usage:
            from agent.context.tokens import _estimate_tokens

            est = _estimate_tokens(answer or "")
            self._send(MsgType.USAGE, {"usage": {"estimated_tokens": est}, "estimated": True})
        else:
            self._send(MsgType.USAGE, {"usage": usage, "estimated": False})
