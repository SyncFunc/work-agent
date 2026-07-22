"""Tier2 工具录像带模型（M6.3）：按录制好的 ``Decision`` 序列顺序回放。

把 ``EventStream`` 当作天然「录像带」：真实跑一轮产生的 events 可序列化为 tape，
CI 用 ``RecordedModel`` 重放，断言工具调用顺序 / 参数 / 错误分支（超时退避、优雅降级）。
确定、零成本，绝不调用真实 LLM。

录制闭环（M6.3 补强，已实现）：
- ``decisions_from_eventstream(stream)``：从一次真实运行的事件流提取决策序列；
- ``dump_tape`` / ``load_tape``：把决策序列落盘 / 加载（JSON 文件）；
- ``RecordedModel.from_tape(path)``：直接由 tape 文件构造回放模型。
这样「真实跑一轮 → 录制 → 落盘 → CI 回放」全链路可自动化、可复现。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from agent.core.events import (
    EventStream,
    EventType,
    _decision_from_dict,
    _decision_to_dict,
)
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

    @classmethod
    def from_tape(cls, path: str | Path) -> RecordedModel:
        """由 tape 文件（``dump_tape`` 产物）构造回放模型。"""
        return cls(load_tape(path))


# --------------------------------------------------------------------------- #
# 录制闭环：事件流 → 决策序列 → tape 文件 → 回放
# --------------------------------------------------------------------------- #
def decisions_from_eventstream(stream: EventStream) -> list[Decision]:
    """从一次真实运行的事件流中提取「模型决策序列」（即录像带内容）。

    只取 ``DECISION`` 事件，顺序即因果顺序；不含 ``usage``（回放只需决策本身）。
    确定、零成本、不依赖是否真实 LLM——可用 ``FakeModel`` 跑出的事件流录制，
    也可以用 nightly 真实 LLM 跑出的事件流录制，下游回放逻辑完全一致。
    """
    return [
        ev.decision
        for ev in stream.all()
        if ev.type == EventType.DECISION and ev.decision is not None
    ]


def dump_tape(decisions: list[Decision], path: str | Path) -> None:
    """把决策序列落盘为 tape 文件（JSON 数组）。

    可被 ``load_tape`` 还原、被 ``RecordedModel.from_tape`` 回放。
    结构同 ``agent.core.events`` 的 ``_decision_to_dict``：``[{"text", "tool_calls"}]``。
    """
    data = [_decision_to_dict(d) for d in decisions]
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_tape(path: str | Path) -> list[Decision]:
    """从 tape 文件加载决策序列（``dump_tape`` 的逆操作）。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [_decision_from_dict(d) for d in raw]
