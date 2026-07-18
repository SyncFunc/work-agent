"""M1.3 验收：ReAct 循环 + 事件流。

覆盖：基础流程、同轮并发、卡死检测、max_iterations 上限、UnknownTool 不崩、事件序列化。
全程用 FakeModel / RecordingModel，不依赖真实 LLM 与真实工具副作用（工具均为内存假工具）。
"""

import asyncio

import pytest

from agent.config.settings import Settings
from agent.core.events import EventStream
from agent.core.loop import AgentLoop, LoopMaxIteration, LoopStalled
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


async def test_max_iterations_hard_cap():
    registry = _make_registry()
    calls = [ToolCall(id="c1", name="echo", arguments={"x": 2})]
    # 永不 final，但把 max_repeat_calls 调高，避免被 stall 抢先
    model = RecordingModel(decision=Decision(tool_calls=calls))
    loop = AgentLoop(model, registry, _settings(max_iterations=5, max_repeat_calls=1000))

    with pytest.raises(LoopMaxIteration):
        await loop.run("never ends")


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


class _RecordingPresenter:
    """验证 loop 把流式文本/工具事件回调给 presenter（不涉及真实 IO）。"""

    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.calls: list[str] = []
        self.results: list[tuple[str, bool]] = []

    def on_text(self, text: str, kind: str) -> None:
        self.texts.append((text, kind))

    def on_tool_call(self, tc) -> None:
        self.calls.append(tc.name)

    def on_tool_result(self, tc, res) -> None:
        self.results.append((tc.name, res.ok))

    def close(self) -> None:
        pass


async def test_presenter_receives_streaming_and_tool_events():
    registry = _make_registry()
    model = FakeModel([
        Decision(tool_calls=[ToolCall(id="c1", name="echo", arguments={"x": 1})]),
        Decision(text="final answer"),
    ])
    presenter = _RecordingPresenter()
    loop = AgentLoop(model, registry, _settings())
    result = await loop.run("task", presenter=presenter)

    # 流式文本（content）回调收到最终答案
    assert presenter.texts == [("final answer", "content")]
    # 工具调用 / 结果回调（按调用顺序）
    assert presenter.calls == ["echo"]
    assert presenter.results == [("echo", True)]
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
    assert not any(e.type == "tool_result" for e in result.events)  # 无有效工具派发
    # 即便空白 name（如 " "）也应被过滤
    noise_ws = Decision(text="x", tool_calls=[ToolCall(id="w", name=" ", arguments={})])
    model2 = FakeModel([noise_ws])
    result2 = await AgentLoop(model2, registry, _settings()).run("t")
    assert result2.iterations == 1
    assert result2.text == "x"

