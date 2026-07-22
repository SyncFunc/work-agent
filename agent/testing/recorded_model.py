"""Tier2 工具录像带模型（M6.3）：按录制好的 ``Decision`` 序列顺序回放。

把 ``EventStream`` 当作天然「录像带」：真实跑一轮产生的 events 可序列化为 tape，
CI 用 ``RecordedModel`` 重放，断言工具调用顺序 / 参数 / 错误分支（超时退避、优雅降级）。
确定、零成本，绝不调用真实 LLM。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from agent.core.model import Decision, Message, StreamEvent


class RecordedModel:
    """测试替身：按给定 ``decisions`` 顺序回放（先入先出）。

    与 ``FakeModel`` 类似但语义更贴近「录制回放」——强调 events 即录像带，
    可直接由真实运行的 ``EventStream`` 推导录制序列，CI 重放验证行为不变。
    """

    def __init__(self, decisions: list[Decision]) -> None:
        self.decisions = list(decisions)
        self.calls: list[list[Message]] = []  # 记录每次决策收到的 messages（便于断言回传内容）

    async def _next(self, messages: list[Message], tools: list[dict] | None = None) -> Decision:
        self.calls.append(list(messages))
        if not self.decisions:
            return Decision(text="<tape exhausted>")
        return self.decisions.pop(0)

    async def act(self, messages: list[Message], tools: list[dict] | None = None) -> Decision:
        return await self._next(messages, tools)

    async def stream(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> AsyncIterator[StreamEvent]:
        d = await self._next(messages, tools)
        if d.text:
            yield StreamEvent(type="text", text=d.text, kind="content")
        yield StreamEvent(type="done", decision=d)
