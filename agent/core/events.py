"""事件流（状态单一事实来源）。

设计要点：
- 循环中所有关键动作（decision / tool_use / tool_result / final / error）都 append 进 EventStream。
- 后续可观测（M5 trace）、恢复（M5 session）、压缩（M3）都从这份事件序列派生。
- Event 可 `to_json()` 序列化、`from_json()` 重放（严格按 seq 恢复因果顺序）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agent.core.model import Decision, ToolCall
from agent.runtime.registry import ToolResult


class EventType(StrEnum):
    """``Event.type`` 的强类型枚举（同时即 JSON 取值，因继承自 ``str``）。

    实际由 loop 产出的集合：
    - ``DECISION`` / ``CLARIFY`` / ``PLAN`` / ``PLAN_PROGRESS`` /
      ``TOOL_USE`` / ``TOOL_RESULT`` / ``FINAL`` / ``ERROR``
    - ``TEXT``（模型文本增量，append 入档持久化）
    - ``TOOL_CALL_DELTA``（工具参数增量，emit 瞬时、不入档，``transient=True``）
    - ``USER``（每轮 ``step`` 的用户输入任务，append 入档；使 EventStream 成为
      完整可重放转录，供 M6.2 会话恢复重建 ``messages``）
    """

    DECISION = "decision"
    CLARIFY = "clarify"
    PLAN = "plan"
    PLAN_PROGRESS = "plan_progress"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    FINAL = "final"
    ERROR = "error"
    TEXT = "text"
    TOOL_CALL_DELTA = "tool_call_delta"
    USER = "user"


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

    type: EventType  # 事件类型，强类型枚举（见 EventType）
    transient: bool = False  # 瞬时事件标记：emit 路径置 True，不进 to_dict/from_dict，不进回放缓冲
    ts: float = 0.0  # 写入时由 EventStream 填充（from_json 重建时保留原值）

    decision: Decision | None = None
    tool_use: ToolCall | None = None
    tool_result: ToolResult | None = None
    tool_call_id: str | None = None
    # 工具调用参数流式增量（tool_call_delta）：实时预览用，瞬时事件、不入档持久化。
    tc_index: int | None = None
    tc_name: str | None = None
    tc_args: str | None = None
    text: str | None = None
    kind: str | None = None  # text 事件：区分 "reasoning"（思考）/ "content"（输出）
    error: str | None = None
    questions: list[dict[str, Any]] | None = (
        None  # clarify 事件：结构化问题清单（list[dict]，JSON 友好）
    )
    plan_path: str | None = None  # plan / plan_progress 事件：计划文件句柄
    plan_update: dict[str, Any] | None = None  # plan_progress 事件：{step_id, status, note}
    seq: int = (
        -1
    )  # 构造时留空，append 时由 EventStream 自动写入（放最后以满足 dataclass 默认值顺序约束）

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"seq": self.seq, "type": self.type.value, "ts": self.ts}
        if self.decision is not None:
            d["decision"] = _decision_to_dict(self.decision)
        if self.tool_use is not None:
            d["tool_use"] = _tool_call_to_dict(self.tool_use)
        if self.tool_result is not None:
            d["tool_result"] = _tool_result_to_dict(self.tool_result)
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        if self.tc_index is not None:
            d["tc_index"] = self.tc_index
        if self.tc_name is not None:
            d["tc_name"] = self.tc_name
        if self.tc_args is not None:
            d["tc_args"] = self.tc_args
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
    def from_dict(cls, d: dict[str, Any]) -> Event:
        return cls(
            seq=d["seq"],
            type=EventType(d["type"]),
            ts=d.get("ts", 0.0),
            decision=_decision_from_dict(d["decision"]) if "decision" in d else None,
            tool_use=_tool_call_from_dict(d["tool_use"]) if "tool_use" in d else None,
            tool_result=_tool_result_from_dict(d["tool_result"]) if "tool_result" in d else None,
            tool_call_id=d.get("tool_call_id"),
            tc_index=d.get("tc_index"),
            tc_name=d.get("tc_name"),
            tc_args=d.get("tc_args"),
            text=d.get("text"),
            kind=d.get("kind"),
            error=d.get("error"),
            questions=d.get("questions"),
            plan_path=d.get("plan_path"),
            plan_update=d.get("plan_update"),
        )


# 事件处理器：订阅后由 append 同步分发（handler 自行决定是否需要异步）。
EventSink = Callable[["Event"], None]


class EventStream:
    """追加式事件流；序列即因果顺序。

    同时是**实时线格式**：``append`` 时同步把事件分发给订阅者（``subscribe``），
    使执行期间外部即可消费（终端渲染 / 未来 web 序列化 ``Event.to_dict()`` 发
    websocket），无需等到 ``run`` 结束才随 ``AgentResult`` 返回。
    """

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._sinks: list[EventSink] = []

    def subscribe(self, sink: EventSink) -> None:
        """注册一个实时事件处理器（渲染/转发）。无去重，多次订阅多次分发。"""
        self._sinks.append(sink)

    def unsubscribe(self, sink: EventSink) -> None:
        """移除已注册的处理器。"""
        try:
            self._sinks.remove(sink)
        except ValueError:
            pass

    def append(self, ev: Event) -> Event:
        # 写入时自动分配 seq（同步、无 await，单线程下原子，并发调工具也安全）。
        ev.seq = len(self._events)
        if ev.ts == 0.0:
            ev.ts = time.time()
        self._events.append(ev)
        # 实时分发：订阅者（终端渲染 / web 转发）在事件落盘后立即收到，无需等待 run 结束。
        for sink in self._sinks:
            sink(ev)
        return ev

    def emit(self, ev: Event) -> None:
        """瞬时分发（**不入档**）：用于实时预览等无需持久化的事件（如 ``tool_call_delta``）。

        与 ``append`` 不同，``emit`` 只把事件推给订阅者，不写入 ``_events``，因此不影响
        ``to_json`` / 重放，也不改变「持久化事件序列」的既有不变量（测试据此断言 type 顺序）。
        """
        ev.transient = True  # 标记为瞬时，回放缓冲（如 daemon 环形缓冲）据此排除
        for sink in self._sinks:
            sink(ev)

    def all(self) -> list[Event]:
        return list(self._events)

    def __iter__(self):
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def to_json(self) -> str:
        return json.dumps([e.to_dict() for e in self._events], ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, s: str) -> EventStream:
        es = cls()
        for d in json.loads(s):
            es._events.append(Event.from_dict(d))
        return es
