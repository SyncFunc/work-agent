"""M1.3 验收：ReAct 循环 + 事件流。

覆盖：基础流程、同轮并发、卡死检测、max_iterations 上限、UnknownTool 不崩、事件序列化。
全程用 FakeModel / RecordingModel，不依赖真实 LLM 与真实工具副作用（工具均为内存假工具）。
"""

import asyncio

import pytest

from agent.config.settings import Settings
from agent.core.events import EventStream
from agent.core.loop import AgentLoop, LoopStalled
from agent.core.model import Decision, FakeModel, RecordingModel, ToolCall
from agent.runtime.registry import ToolRegistry, ToolResult, tool


# --------------------------------------------------------------------------- #
# 测试夹具：内存假工具 + 假注册表
# --------------------------------------------------------------------------- #
async def _echo(args: dict) -> ToolResult:
    return ToolResult(ok=True, output=str(args))


async def _slow(args: dict) -> ToolResult:
    await asyncio.sleep(args.get("dt", 0.1))
    return ToolResult(ok=True, output=args.get("tag", ""))


def _make_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(tool("echo", risk="read")(_echo))
    r.register(tool("slow", risk="read")(_slow))
    return r


def _settings(**kw) -> Settings:
    base = dict(max_iterations=20, max_tool_concurrency=5, max_repeat_calls=3)
    base.update(kw)
    return Settings(**base)


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #
async def test_basic_flow_records_events():
    registry = _make_registry()
    model = FakeModel(
        [
            Decision(tool_calls=[ToolCall(id="c1", name="echo", arguments={"x": 1})]),
            Decision(text="final answer"),
        ]
    )
    loop = AgentLoop(model, registry, _settings())

    result = await loop.run("do the task")

    assert result.text == "final answer"
    assert result.iterations == 2

    types = [e.type for e in result.events]
    # decision -> tool_use -> tool_result -> (流式 text) -> decision -> final
    assert types == ["decision", "tool_use", "tool_result", "text", "decision", "final"]

    # 流式文本事件内容正确（最终答案的逐片文本）
    text_ev = next(e for e in result.events if e.type == "text")
    assert text_ev.text == "final answer"

    res_ev = next(e for e in result.events if e.type == "tool_result")
    assert res_ev.tool_result is not None and res_ev.tool_result.ok
    assert res_ev.tool_call_id == "c1"


async def test_concurrent_tools_run_in_parallel_and_pair_by_id():
    registry = _make_registry()
    # 同轮两个 slow(0.1) 调用：并发应 ~0.1s，顺序则 ~0.2s
    calls = [
        ToolCall(id="c1", name="slow", arguments={"dt": 0.1, "tag": "A"}),
        ToolCall(id="c2", name="slow", arguments={"dt": 0.1, "tag": "B"}),
    ]
    model = FakeModel([Decision(tool_calls=calls), Decision(text="done")])
    loop = AgentLoop(model, registry, _settings(max_tool_concurrency=5))

    async def run():
        return await loop.run("concurrent")

    res = await asyncio.wait_for(run(), timeout=0.25)
    # 若未并发，0.2s 的串行会触发 wait_for 超时
    assert res.text == "done"

    # 结果按 tool_call_id 正确配对
    tr = {e.tool_call_id: e.tool_result for e in res.events if e.type == "tool_result"}
    assert tr["c1"].output == "A"
    assert tr["c2"].output == "B"

    # 该轮所有 tool_use 事件都在 tool_result 之前
    idx_use = [i for i, e in enumerate(res.events) if e.type == "tool_use"]
    idx_res = [i for i, e in enumerate(res.events) if e.type == "tool_result"]
    assert idx_use and idx_res and max(idx_use) < min(idx_res)


async def test_stall_detected_on_repeated_identical_calls():
    counter = {"n": 0}

    async def _counting(args: dict) -> ToolResult:
        counter["n"] += 1
        return ToolResult(ok=True, output="x")

    registry = ToolRegistry()
    registry.register(tool("echo", risk="read")(_counting))
    calls = [ToolCall(id="c1", name="echo", arguments={"x": 1})]
    # RecordingModel 永远返回相同 tool_calls（绝不 final），触发卡死
    model = RecordingModel(decision=Decision(tool_calls=calls))
    loop = AgentLoop(model, registry, _settings(max_repeat_calls=3, max_tool_concurrency=5))

    with pytest.raises(LoopStalled):
        await loop.run("loop forever")

    # 卡死检测在「执行后」进行：第 4 次重复调用执行后 repeat_count 达 3 触发，
    # 故工具共被执行 4 次。
    assert counter["n"] == 4


async def test_max_iterations_soft_limit_returns_result():
    """回归（M1.x 修复）：max_iterations 触顶不再抛 LoopMaxIteration 中断会话，而是软返回
    ——带「已达最大轮次」提示、标记 soft_limit_hit、并把累计上下文（messages）交回，使会话
    可续（上层 chat REPL 自然进入下一轮，用户接棒续跑，不丢失历史）。"""
    registry = _make_registry()
    calls = [ToolCall(id="c1", name="echo", arguments={"x": 2})]
    # 永不 final，但把 max_repeat_calls 调高，避免被 stall 抢先
    model = RecordingModel(decision=Decision(tool_calls=calls))
    loop = AgentLoop(model, registry, _settings(max_iterations=5, max_repeat_calls=1000))

    result = await loop.run("never ends")

    assert result.soft_limit_hit is True
    assert result.text and "最大轮次" in result.text
    assert result.iterations == 5
    # 关键：累计上下文已交回，且自洽（最后一轮的 tool 回执齐全），可直接作为下一轮输入。
    assert result.messages is not None
    last = result.messages[-1]
    assert last.role == "tool"  # 末轮 tool 调用已配对回执，无悬空 tool_calls
    # 与 session 续接一致：下一轮 run 用该 messages 不会触发协议 400、不抛异常。
    # （loop 构造时已带 max_repeat_calls=1000，模型永不 final 会再次软返回，但不再崩溃）
    cont = await loop.run("继续", result.messages)
    assert cont.messages is not None  # 续跑成功（不抛、不崩）
    assert cont.soft_limit_hit is True


async def test_unknown_tool_does_not_crash_loop():
    registry = _make_registry()  # 没有 "ghost" 工具
    model = FakeModel(
        [
            Decision(tool_calls=[ToolCall(id="c1", name="ghost", arguments={})]),
            Decision(text="recovered"),
        ]
    )
    loop = AgentLoop(model, registry, _settings())

    result = await loop.run("call unknown")
    assert result.text == "recovered"  # 循环未中断，模型下一轮给出 final

    tr = next(e for e in result.events if e.type == "tool_result")
    assert tr.tool_result is not None
    assert not tr.tool_result.ok
    assert "unknown tool" in (tr.tool_result.error or "")


async def test_event_stream_roundtrip_json():
    registry = _make_registry()
    model = FakeModel(
        [
            Decision(tool_calls=[ToolCall(id="c1", name="echo", arguments={"x": 1})]),
            Decision(text="final answer"),
        ]
    )
    loop = AgentLoop(model, registry, _settings())
    result = await loop.run("serialize me")

    js = result.events.to_json()
    rebuilt = EventStream.from_json(js)

    assert len(rebuilt) == len(result.events)
    assert [e.type for e in rebuilt] == [e.type for e in result.events]
    # 嵌套结构保真：决策里的 tool_calls 完整还原
    first_decision = next(e for e in rebuilt if e.type == "decision")
    assert first_decision.decision is not None
    assert first_decision.decision.tool_calls[0].name == "echo"
    assert first_decision.decision.tool_calls[0].arguments == {"x": 1}
    # seq 严格保真（因果顺序）
    assert [e.seq for e in rebuilt] == list(range(len(rebuilt)))


async def test_usage_accumulates_across_iterations():
    """每轮 model 调用的 token 用量在 AgentResult.usage 中累加（M1.6 验收）。"""
    registry = _make_registry()
    model = FakeModel([
        Decision(
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"x": 1})],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        ),
        Decision(text="final answer", usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}),
    ])
    loop = AgentLoop(model, registry, _settings())
    result = await loop.run("task")

    assert result.usage == {"prompt_tokens": 30, "completion_tokens": 13, "total_tokens": 43}


class _EventRecordingTransport:
    """验证 loop 把流式文本/工具事件经 EventStream 分发（订阅驱动，不涉及真实 IO）。"""

    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.calls: list[str] = []
        self.results: list[tuple[str, bool]] = []

    def bind(self, stream):
        def sink(ev):
            if ev.type == "text":
                self.texts.append((ev.text, ev.kind))
            elif ev.type == "tool_use":
                self.calls.append(ev.tool_use.name)
            elif ev.type == "tool_result":
                self.results.append((ev.tool_call_id, ev.tool_result.ok))
        stream.subscribe(sink)


async def test_transport_receives_streaming_and_tool_events():
    registry = _make_registry()
    model = FakeModel([
        Decision(tool_calls=[ToolCall(id="c1", name="echo", arguments={"x": 1})]),
        Decision(text="final answer"),
    ])
    transport = _EventRecordingTransport()
    loop = AgentLoop(model, registry, _settings())
    result = await loop.run("task", transport=transport)

    # 流式文本（content）经事件分发收到最终答案
    assert transport.texts == [("final answer", "content")]
    # 工具调用 / 结果经 tool_use / tool_result 事件分发（按调用顺序）
    assert transport.calls == ["echo"]
    assert transport.results == [("c1", True)]
    # 循环内部不调用 close（生命周期由 CLI 控制）
    assert result.usage == {}


async def test_empty_name_toolcall_treated_as_final():
    """回归（M1.3 防失控）：DeepSeek/OpenAI 在「带 tools 的纯文本回复」时，偶尔会在
    流式末尾附带 name 为空的 tool_call 噪声。若不丢弃，decision.tool_calls 非空 →
    is_final=False → 落入执行分支（空 name 被当 UnknownTool 降级），模型下一轮又输出
    相同文本，造成「纯文本刷屏」死循环。

    修复后：空 name tool_call 在 _decide 收尾被过滤，纯文本回复的 tool_calls 为空 →
    is_final=True → 直接作为 final 返回，循环只迭代一次、不派发任何工具。
    """
    registry = _make_registry()
    noise = Decision(
        text="这是审核计划",
        tool_calls=[ToolCall(id="n1", name="", arguments={})],
    )
    # 多轮相同噪声：未修复会反复刷屏（并最终被 stall/max_iterations 中断而非正常 final）
    model = FakeModel([noise, noise, noise, noise, noise])
    loop = AgentLoop(model, registry, _settings(max_iterations=20))
    result = await loop.run("review loop.py")

    assert result.text == "这是审核计划"
    assert result.iterations == 1  # 关键：只迭代一次，未陷入刷屏


async def test_tool_call_delta_streamed_to_transport():
    """回归（write 流式输出）：模型流式产出工具调用参数（tool_call_delta）时，loop 把
    增量作为瞬时事件（emit，不入档）分发给订阅方，使 UI 能在参数生成过程中实时预览
    （如 write/edit 的 content），而非等到决策收尾才一次性出现。"""
    from agent.core.model import StreamEvent

    class _DeltaModel:
        def __init__(self) -> None:
            self.n = 0

        async def stream(self, messages, tools=None):
            self.n += 1
            if self.n == 1:
                yield StreamEvent(type="tool_call_delta", tc_index=0, tc_name="echo",
                                  tc_args='{"x": 1')
                yield StreamEvent(type="tool_call_delta", tc_index=0, tc_name="echo",
                                  tc_args='{"x": 123}')
                yield StreamEvent(type="done", decision=Decision(
                    tool_calls=[ToolCall(id="c1", name="echo", arguments={"x": 123})]))
            else:
                yield StreamEvent(type="done", decision=Decision(text="done"))

    deltas = []

    class _EventSpy:
        def bind(self, stream):
            def sink(ev):
                if ev.type == "tool_call_delta":
                    deltas.append((ev.tc_index, ev.tc_name, ev.tc_args))
            stream.subscribe(sink)

    loop = AgentLoop(_DeltaModel(), _make_registry(), _settings())
    await loop.run("use a tool", transport=_EventSpy())

    assert len(deltas) == 2
    assert deltas[0][1] == "echo"
    assert '{"x": 1' in deltas[0][2]
    assert "123" in deltas[1][2]
    # 即便空白 name（如 " "）也应被过滤
    noise_ws = Decision(text="x", tool_calls=[ToolCall(id="w", name=" ", arguments={})])
    model2 = FakeModel([noise_ws])
    result2 = await AgentLoop(model2, _make_registry(), _settings()).run("t")
    assert result2.iterations == 1
    assert result2.text == "x"

