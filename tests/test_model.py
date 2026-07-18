"""M1.1 验收（重构后）：provider 无关模型抽象 + 流式 + 配置读取。"""

from types import SimpleNamespace

from agent.config.settings import Settings
from agent.core.model import (
    Decision,
    FakeModel,
    Message,
    OpenAICompatibleModel,
    RecordingModel,
    StreamEvent,
    ToolCall,
)


# ---------------- 非流式 ---------------- #
async def test_fake_model_returns_script_in_order():
    m = FakeModel(
        [
            Decision(text="a"),
            Decision(tool_calls=[ToolCall(id="1", name="bash", arguments={"cmd": "echo hi"})]),
        ]
    )
    d1 = await m.act([])
    d2 = await m.act([])
    assert d1.text == "a"
    assert d2.tool_calls[0].name == "bash"
    assert len(m.calls) == 2


async def test_recording_model_records_messages():
    m = RecordingModel(decision=Decision(text="ok"))
    await m.act([Message(role="user", content="hi")])
    assert len(m.calls) == 1
    assert m.calls[0][0].content == "hi"


# ---------------- 流式 ---------------- #
async def test_fake_model_stream_yields_text_then_done():
    m = FakeModel([Decision(text="hello")])
    events = [e async for e in m.stream([])]
    assert events[0].type == "text" and events[0].text == "hello"
    assert events[-1].type == "done" and events[-1].decision.is_final


# ---------------- 可注入的假 OpenAI client ---------------- #
class _FakeCompletions:
    def __init__(self, chunks, completion):
        self._chunks = chunks
        self._completion = completion

    async def create(self, **kwargs):
        if kwargs.get("stream"):
            async def gen():
                for c in self._chunks:
                    yield c

            return gen()
        return self._completion


class _FakeChat:
    def __init__(self, chunks, completion):
        self.completions = _FakeCompletions(chunks, completion)


class _FakeClient:
    def __init__(self, chunks=None, completion=None):
        self.chat = _FakeChat(chunks or [], completion)


def _chunk(delta):
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _delta(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _completion(text="final", tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=tool_calls))])


async def test_openai_model_act_returns_decision():
    client = _FakeClient(completion=_completion(text="final"))
    m = OpenAICompatibleModel(api_key="x", base_url="u", model="m", client=client)
    d = await m.act([])
    assert d.text == "final" and d.is_final


async def test_openai_model_stream_accumulates_text_and_tool_calls():
    # 模拟流式分片：文本分一片；工具调用分两片（name + arguments 增量）
    tc0 = SimpleNamespace(index=0, id="call_1", function=SimpleNamespace(name="bash", arguments='{"cmd":"'))
    tc0b = SimpleNamespace(index=0, id=None, function=SimpleNamespace(name=None, arguments='echo hi"}'))
    chunks = [
        _chunk(_delta(content="hi")),
        _chunk(_delta(tool_calls=[tc0])),
        _chunk(_delta(tool_calls=[tc0b])),
    ]
    client = _FakeClient(chunks=chunks)
    m = OpenAICompatibleModel(api_key="x", base_url="u", model="m", client=client)

    text = ""
    done: Decision | None = None
    async for ev in m.stream([]):
        if ev.type == "text":
            text += ev.text
        elif ev.type == "done":
            done = ev.decision

    assert text == "hi"
    assert done is not None
    assert done.tool_calls[0].name == "bash"
    assert done.tool_calls[0].arguments == {"cmd": "echo hi"}


# ---------------- 配置（provider 无关） ---------------- #
def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-flash")
    s = Settings()
    assert s.llm_api_key == "sk-test"
    assert s.llm_model == "deepseek-v4-flash"


def test_settings_default_model_is_v4_flash():
    s = Settings(llm_api_key="sk-x")
    assert s.llm_model == "deepseek-v4-flash"


def test_model_builds_from_settings_offline():
    s = Settings(llm_api_key="sk-x", llm_model="deepseek-v4-flash")
    m = OpenAICompatibleModel.from_settings(s)
    assert isinstance(m, OpenAICompatibleModel)
    assert m.model == "deepseek-v4-flash"
