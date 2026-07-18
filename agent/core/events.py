"""事件流（状态单一事实来源）。

设计要点：
- 循环中所有关键动作（decision / tool_use / tool_result / final / error）都 append 进 EventStream。
- 后续可观测（M5 trace）、恢复（M5 session）、压缩（M3）都从这份事件序列派生。
- Event 可 `to_json()` 序列化、`from_json()` 重放（严格按 seq 恢复因果顺序）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from agent.core.model import Decision, ToolCall
from agent.runtime.registry import ToolResult


# --------------------------------------------------------------------------- #
# 嵌套结构 <-> 字典（events 是纯数据，需能在 JSON 间往返）
# --------------------------------------------------------------------------- #
def _tool_call_to_dict(tc: ToolCall) -> dict[str, Any]:
    return {"id": tc.id, "name": tc.name, "arguments": tc.arguments}


def _tool_call_from_dict(d: dict[str, Any]) -> ToolCall:
    return ToolCall(id=d["id"], name=d["name"], arguments=d.get("arguments", {}))


def _decision_to_dict(d: Decision) -> dict[str, Any]:
    return {
        "text": d.text,
        "tool_calls": [_tool_call_to_dict(tc) for tc in d.tool_calls],
    }


def _decision_from_dict(d: dict[str, Any]) -> Decision:
    return Decision(
        text=d.get("text"),
        tool_calls=[_tool_call_from_dict(tc) for tc in d.get("tool_calls", [])],
    )


def _tool_result_to_dict(r: ToolResult) -> dict[str, Any]:
    return {"ok": r.ok, "output": r.output, "error": r.error}


def _tool_result_from_dict(d: dict[str, Any]) -> ToolResult:
    return ToolResult(ok=d["ok"], output=d.get("output", ""), error=d.get("error"))


@dataclass
class Event:
    """事件流中的一条记录。顺序由 `seq` 唯一确定（append 时自动写入）。"""

    type: str  # decision | tool_use | tool_result | final | error | plan | plan_progress
    ts: float = 0.0  # 写入时由 EventStream 填充（from_json 重建时保留原值）

    decision: Decision | None = None
    tool_use: ToolCall | None = None
    tool_result: ToolResult | None = None
    tool_call_id: str | None = None
    text: str | None = None
    kind: str | None = None  # text 事件：区分 "reasoning"（思考）/ "content"（输出）
    error: str | None = None
    questions: list[dict[str, Any]] | None = None  # clarify 事件：结构化问题清单（list[dict]，JSON 友好）
    plan_path: str | None = None                   # plan / plan_progress 事件：计划文件句柄
    plan_update: dict[str, Any] | None = None       # plan_progress 事件：{step_id, status, note}
    seq: int = -1  # 构造时留空，append 时由 EventStream 自动写入（放最后以满足 dataclass 默认值顺序约束）

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"seq": self.seq, "type": self.type, "ts": self.ts}
        if self.decision is not None:
            d["decision"] = _decision_to_dict(self.decision)
        if self.tool_use is not None:
            d["tool_use"] = _tool_call_to_dict(self.tool_use)
        if self.tool_result is not None:
            d["tool_result"] = _tool_result_to_dict(self.tool_result)
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        if self.text is not None:
            d["text"] = self.text
        if self.kind is not None:
            d["kind"] = self.kind
        if self.error is not None:
            d["error"] = self.error
        if self.questions is not None:
            d["questions"] = self.questions
        if self.plan_path is not None:
            d["plan_path"] = self.plan_path
        if self.plan_update is not None:
            d["plan_update"] = self.plan_update
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        return cls(
            seq=d["seq"],
            type=d["type"],
            ts=d.get("ts", 0.0),
            decision=_decision_from_dict(d["decision"]) if "decision" in d else None,
            tool_use=_tool_call_from_dict(d["tool_use"]) if "tool_use" in d else None,
            tool_result=_tool_result_from_dict(d["tool_result"]) if "tool_result" in d else None,
            tool_call_id=d.get("tool_call_id"),
            text=d.get("text"),
            kind=d.get("kind"),
            error=d.get("error"),
            questions=d.get("questions"),
            plan_path=d.get("plan_path"),
            plan_update=d.get("plan_update"),
        )


class EventStream:
    """追加式事件流；序列即因果顺序。"""

    def __init__(self) -> None:
        self._events: list[Event] = []

    def append(self, ev: Event) -> Event:
        # 写入时自动分配 seq（同步、无 await，单线程下原子，并发调工具也安全）。
        ev.seq = len(self._events)
        if ev.ts == 0.0:
            ev.ts = time.time()
        self._events.append(ev)
        return ev

    def all(self) -> list[Event]:
        return list(self._events)

    def __iter__(self):
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def to_json(self) -> str:
        return json.dumps([e.to_dict() for e in self._events], ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, s: str) -> "EventStream":
        es = cls()
        for d in json.loads(s):
            es._events.append(Event.from_dict(d))
        return es
