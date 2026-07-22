"""M1.5 验收：意图澄清（模糊任务先问后做）。

覆盖（详见 milestones/M1-骨架/1.5-意图澄清.md）：
- 澄清提前返回：模型调 ask_clarification → needs_clarification=True，且澄清前不执行任何工具。
- 注入答案重跑 → 进入正常执行，不再 early-return。
- 防呆：连续追问超过 max_clarify_rounds → 第 max+1 次不再 early-return（防死循环）。
- 关闭澄清：clarify_enabled=False → 即便模型调 ask_clarification 也不提前返回（降级为未知工具）。
- clarify 事件 JSON 往返保真；options/multiSelect 正确解析。
全程用 FakeModel / RecordingModel + 假注册表，不依赖真实 LLM。
"""

from types import SimpleNamespace

from agent.config.settings import Settings
from agent.core.intent import ASK_CLARIFICATION_TOOL_NAME, extract_clarify
from agent.core.loop import AgentLoop
from agent.core.model import (
    Decision,
    FakeModel,
    Message,
    OpenAICompatibleModel,
    ToolCall,
)
from agent.runtime.registry import ToolRegistry, ToolResult, tool


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _make_registry() -> ToolRegistry:
    r = ToolRegistry()

    async def _echo(args: dict) -> ToolResult:
        return ToolResult(ok=True, output=str(args))

    r.register(tool("echo", risk="read")(_echo))
    return r


def _settings(**kw) -> Settings:
    loop = dict(max_iterations=20, max_tool_concurrency=5, max_repeat_calls=3)
    for k in (
        "max_iterations",
        "max_tool_concurrency",
        "max_repeat_calls",
        "max_tool_output_chars",
    ):
        if k in kw:
            loop[k] = kw.pop(k)
    clarify = dict(enabled=True, max_rounds=2)
    if "clarify_enabled" in kw:
        clarify["enabled"] = kw.pop("clarify_enabled")
    if "max_clarify_rounds" in kw:
        clarify["max_rounds"] = kw.pop("max_clarify_rounds")
    return Settings(loop=loop, clarify=clarify, **kw)


def _ask_clarify(q: dict) -> Decision:
    return Decision(
        tool_calls=[
            ToolCall(id="cq", name=ASK_CLARIFICATION_TOOL_NAME, arguments={"questions": [q]})
        ]
    )


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #
async def test_clarify_early_returns_without_executing_tools():
    registry = _make_registry()
    model = FakeModel(
        [
            _ask_clarify(
                {"question": "用哪个框架？", "options": ["FastAPI", "Flask"], "multiSelect": False}
            ),
        ]
    )
    loop = AgentLoop(model, registry, _settings())

    result = await loop.run("帮我搭个 Web 服务")

    assert result.needs_clarification is True
    assert result.questions is not None and len(result.questions) == 1
    assert result.questions[0].question == "用哪个框架？"
    assert result.questions[0].options == ["FastAPI", "Flask"]
    assert result.questions[0].multiSelect is False

    types = [e.type for e in result.events]
    # 澄清前绝不能有任何工具执行
    assert "tool_use" not in types
    assert "tool_result" not in types
    assert "clarify" in types
    # 模型收到了 ask_clarification 控制工具
    assert model.tools_seen and any(
        t and any(x.get("function", {}).get("name") == ASK_CLARIFICATION_TOOL_NAME for x in t)
        for t in model.tools_seen
    )


def _assert_toolcalls_have_receipts(messages: list) -> None:
    """校验 OpenAI/DeepSeek 协议：每个带 tool_calls 的 assistant 消息之后，都必须紧跟
    对应每个 tool_call_id 的 tool 回执。否则真实 API 会返回 400（insufficient tool
    messages following tool_calls）。这里做与协议一致的顺序校验。
    """
    i = 0
    while i < len(messages):
        m = messages[i]
        if m.role == "assistant" and m.tool_calls:
            expected = {tc.id for tc in m.tool_calls}
            got: set = set()
            j = i + 1
            while j < len(messages) and messages[j].role == "tool":
                got.add(messages[j].tool_call_id)
                j += 1
            assert expected <= got, (
                f"assistant tool_calls {expected} 缺少 tool 回执，实际紧随的回执={got}"
            )
            i = j
        else:
            i += 1


async def test_clarify_messages_have_tool_receipt_for_protocol():
    """回归（400 修复）：澄清提前返回时，conv 里 assistant(tool_calls=[ask_clarification])
    之后必须补一条对应的 tool 回执；否则会话层把答案作为新 user 消息续跑时，消息序列
    「assistant(tool_calls) → user」会让 OpenAI/DeepSeek 报 400。
    """
    registry = _make_registry()
    model = FakeModel([_ask_clarify({"question": "用哪个框架？", "options": ["A", "B"]})])
    loop = AgentLoop(model, registry, _settings())

    r1 = await loop.run("帮我搭个 Web 服务")
    assert r1.needs_clarification is True
    assert r1.messages is not None
    # 关键：末尾应为 assistant(tool_calls) + tool 回执，而非光秃秃的 assistant(tool_calls)
    _assert_toolcalls_have_receipts(r1.messages)
    tail = r1.messages[-1]
    assert tail.role == "tool" and tail.tool_call_id == "cq"

    # 会话层续接：带旧 messages + 答案作为新 user 任务再跑一轮，序列仍合法
    model2 = FakeModel([Decision(text="done")])
    loop.model = model2
    r2 = await loop.run("用哪个框架？: A", r1.messages, clarify_total=r1.clarify_total)
    assert r2.text == "done"
    _assert_toolcalls_have_receipts(r2.messages or [])


async def test_rerun_with_answer_proceeds_normally():
    registry = _make_registry()
    # 重跑时模型不再澄清，直接给出最终答案
    model = FakeModel([Decision(text="已按你的选择继续完成。")])
    loop = AgentLoop(model, registry, _settings())

    result = await loop.run("用 FastAPI，继续")

    assert result.needs_clarification is False
    assert result.text == "已按你的选择继续完成。"


async def test_anti_stuck_after_max_clarify_rounds():
    settings = _settings(clarify_enabled=True, max_clarify_rounds=2)
    loop = AgentLoop(
        FakeModel(
            [
                _ask_clarify({"question": "Q?"}),
                _ask_clarify({"question": "Q?"}),
                _ask_clarify({"question": "Q?"}),
                Decision(text="forced final"),
            ]
        ),
        _make_registry(),
        settings,
    )

    # 跨 run 续接由会话层负责：传入旧 messages 与累计的 clarify_total，并用回传值更新。
    msgs: list = []
    ct = 0
    r1 = await loop.run("vague task", msgs, clarify_total=ct)
    msgs, ct = r1.messages, r1.clarify_total
    r2 = await loop.run("vague task", msgs, clarify_total=ct)
    msgs, ct = r2.messages, r2.clarify_total
    r3 = await loop.run("vague task", msgs, clarify_total=ct)

    assert r1.needs_clarification is True  # 第 1 轮：提前返回
    assert r2.needs_clarification is True  # 第 2 轮：仍在限额内
    # 第 3 轮累计超过 max -> 不再提前返回，落入执行（ask 作为未知工具降级）→ 最终 final
    assert r3.needs_clarification is False
    assert r3.text == "forced final"


async def test_clarify_disabled_does_not_early_return():
    registry = _make_registry()
    model = FakeModel(
        [
            _ask_clarify({"question": "Q?"}),
            Decision(text="recovered"),
        ]
    )
    loop = AgentLoop(model, registry, _settings(clarify_enabled=False))

    result = await loop.run("do something vague")

    assert result.needs_clarification is False
    assert result.text == "recovered"
    # ask_clarification 作为未知工具被降级，产生 tool_result(error 含 unknown tool)
    tr = next(e for e in result.events if e.type == "tool_result")
    assert tr.tool_result is not None and not tr.tool_result.ok


async def test_clarify_event_roundtrips_json():
    registry = _make_registry()
    model = FakeModel(
        [_ask_clarify({"question": "确认范围？", "options": ["A", "B"], "multiSelect": True})]
    )
    loop = AgentLoop(model, registry, _settings())

    result = await loop.run("任务模糊")
    js = result.events.to_json()
    rebuilt = type(result.events).from_json(js)

    clar = next(e for e in rebuilt if e.type == "clarify")
    assert clar.questions is not None
    assert clar.questions[0]["question"] == "确认范围？"
    assert clar.questions[0]["options"] == ["A", "B"]
    assert clar.questions[0]["multiSelect"] is True


def test_extract_clarify_parses_options_and_multiselect():
    decision = Decision(
        tool_calls=[
            ToolCall(
                id="c1",
                name=ASK_CLARIFICATION_TOOL_NAME,
                arguments={
                    "questions": [
                        {"question": "Q1", "options": ["x", "y"], "multiSelect": True},
                        {"question": "Q2"},
                    ]
                },
            )
        ]
    )
    qs = extract_clarify(decision)
    assert qs is not None
    assert len(qs) == 2
    assert qs[0].question == "Q1" and qs[0].options == ["x", "y"] and qs[0].multiSelect is True
    assert qs[1].question == "Q2" and qs[1].options is None


def test_extract_clarify_priority_over_other_tools():
    # 同轮混有澄清与其它工具调用：澄清优先，忽略其它调用
    decision = Decision(
        tool_calls=[
            ToolCall(id="c1", name="echo", arguments={}),
            ToolCall(
                id="c2",
                name=ASK_CLARIFICATION_TOOL_NAME,
                arguments={"questions": [{"question": "先问？"}]},
            ),
        ]
    )
    qs = extract_clarify(decision)
    assert qs is not None and qs[0].question == "先问？"


def test_extract_clarify_returns_none_without_clarification():
    decision = Decision(tool_calls=[ToolCall(id="c1", name="echo", arguments={})])
    assert extract_clarify(decision) is None


# --------------------------------------------------------------------------- #
# 模型层：tools 参数透传（M1.5 依赖的共享基础设施）
# --------------------------------------------------------------------------- #
async def test_fake_model_records_tools_passthrough():
    model = FakeModel([Decision(text="ok")])
    await model.act(
        [Message(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "ask_clarification"}}],
    )
    assert model.tools_seen and model.tools_seen[0] is not None
    assert model.tools_seen[0][0]["function"]["name"] == "ask_clarification"


async def test_openai_model_passes_tools_to_create():
    captured: dict = {}

    class _Completions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="final", tool_calls=None))]
            )

    class _Chat:
        def __init__(self):
            self.completions = _Completions()

    class _Client:
        def __init__(self):
            self.chat = _Chat()

    tools = [{"type": "function", "function": {"name": "ask_clarification"}}]
    model = OpenAICompatibleModel(api_key="x", base_url="u", model="m", client=_Client())
    await model.act([Message(role="user", content="hi")], tools=tools)

    assert "tools" in captured
    assert captured["tools"] == tools
