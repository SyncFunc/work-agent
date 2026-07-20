"""M5.2 验收：SubagentSpawner（发现 / 隔离 / 白名单 / 模型降级 / 深度 / trace 父子 / fork /
子任务渲染 / 内置类型）。

全程用 FakeModel / RecordingModel，不依赖真实 LLM、真实工具副作用或真实终端交互。
"""

import asyncio

import pytest

from agent.config.settings import Settings
from agent.core.events import EventStream
from agent.core.loop import AgentLoop
from agent.core.model import Decision, FakeModel, Message, RecordingModel, ToolCall
from agent.core.transport import AgentTransport
from agent.obs.tracer import Tracer
from agent.runtime.approval import Action
from agent.runtime.registry import ToolRegistry, ToolResult, tool
from agent.subagent import (
    BUILTIN_EXPLORE,
    BUILTIN_GENERAL,
    BUILTIN_PLAN,
    AgentSpec,
    SubagentSpawner,
    _SubAgentTransport,
)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
async def _echo(args: dict) -> ToolResult:
    return ToolResult(ok=True, output=str(args))


async def _noop_write(args: dict) -> ToolResult:
    return ToolResult(ok=True, output="written")


def _registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(tool("echo", risk="read")(_echo))
    r.register(tool("write", risk="edit")(_noop_write))
    return r


def _settings(**kw) -> Settings:
    loop = dict(max_iterations=20, max_tool_concurrency=5, max_repeat_calls=3)
    loop.update(kw.pop("loop", {}))
    return Settings(loop=loop, **kw)


class _FakeParentTransport:
    """记录交付给它的事件；模拟父 agent 的终端传输。"""

    def __init__(self, interactive: bool = True) -> None:
        self._interactive = interactive
        self.rendered: list = []
        self.asked: list = []
        self.approved: list = []

    @property
    def interactive(self) -> bool:
        return self._interactive

    def bind(self, stream):
        stream.subscribe(lambda ev: self.rendered.append(ev))

    async def ask(self, question):
        self.asked.append(question)
        return "answered"

    async def approve(self, action: Action) -> bool:
        self.approved.append(action)
        return True

    def show_questions(self, questions): pass
    def show_plan(self, res): pass
    async def confirm_plan(self): return True
    def notify(self, message): pass
    def close(self): pass
    def report_usage(self, usage, answer=None): pass


# --------------------------------------------------------------------------- #
# 发现 / 内置类型
# --------------------------------------------------------------------------- #
def test_discover_returns_builtin_types():
    spawner = SubagentSpawner(_settings())
    specs = spawner.discover()
    names = {s.name for s in specs}
    assert {"explore", "plan", "general-purpose"}.issubset(names)
    # 内置只读类型拒绝 write/edit
    explore = next(s for s in specs if s.name == "explore")
    assert "write" not in (explore.tools or [])
    assert "edit" in explore.disallowed_tools
    assert explore.permission_mode == "plan"


def test_get_returns_spec():
    spawner = SubagentSpawner(_settings())
    assert spawner.get("explore") is not None
    assert spawner.get("nonexistent") is None


def test_parse_agent_file(tmp_path):
    f = tmp_path / "custom.md"
    f.write_text(
        "---\n"
        "name: my-agent\ndescription: demo\n"
        "tools: [echo]\ndisallowed_tools: [write]\n"
        "model: deepseek-chat\npermission_mode: plan\n"
        "max_turns: 7\nshare_history: true\n"
        "---\n"
        "你是一个自定义 agent。\n",
        encoding="utf-8",
    )
    spawner = SubagentSpawner(_settings())
    spec = spawner._parse_agent_file(f)
    assert spec is not None
    assert spec.name == "my-agent"
    assert spec.tools == ["echo"]
    assert spec.disallowed_tools == ["write"]
    assert spec.model == "deepseek-chat"
    assert spec.permission_mode == "plan"
    assert spec.max_turns == 7
    assert spec.share_history is True
    assert spec.system_prompt == "你是一个自定义 agent。"


# --------------------------------------------------------------------------- #
# 隔离 / 摘要
# --------------------------------------------------------------------------- #
async def test_spawn_returns_summary_and_is_isolated():
    spawner = SubagentSpawner(_settings())
    model = FakeModel([Decision(text="subagent final")])
    res = await spawner.spawn(
        BUILTIN_GENERAL, "do subtask", base_registry=_registry(), base_model=model
    )
    assert res.text == "subagent final"
    # 独立上下文：子 messages 从 [user(task)] 开始（无父历史）
    assert res.messages[0].role == "user"
    assert res.messages[0].content == "do subtask"
    assert res.messages[-1].role == "assistant"
    assert res.messages[-1].content == "subagent final"


async def test_fork_copies_parent_messages():
    spawner = SubagentSpawner(_settings())
    model = FakeModel([Decision(text="compacted")])
    parent_conv = [Message(role="user", content="PARENT_SECRET_CONTEXT")]
    res = await spawner.spawn(
        AgentSpec(name="mem", description="记忆子 agent",
                  system_prompt="提取摘要", tools=None, share_history=True),
        "summarize", base_registry=_registry(), base_model=model,
        parent_messages=parent_conv,
    )
    contents = [m.content for m in res.messages]
    assert "PARENT_SECRET_CONTEXT" in contents


async def test_no_fork_excludes_parent_messages():
    spawner = SubagentSpawner(_settings())
    model = FakeModel([Decision(text="done")])
    parent_conv = [Message(role="user", content="PARENT_SECRET_CONTEXT")]
    res = await spawner.spawn(
        BUILTIN_GENERAL, "do subtask", base_registry=_registry(), base_model=model,
        parent_messages=parent_conv,  # share_history=False → 忽略
    )
    contents = [m.content for m in res.messages]
    assert "PARENT_SECRET_CONTEXT" not in contents


# --------------------------------------------------------------------------- #
# 工具白名单
# --------------------------------------------------------------------------- #
async def test_tool_whitelist_drops_disallowed():
    spawner = SubagentSpawner(_settings())
    # 子 agent 只允许 echo；调用 write → unknown tool 被降级
    model = FakeModel([
        Decision(tool_calls=[ToolCall(id="c1", name="write", arguments={"path": "x"})]),
        Decision(text="recovered"),
    ])
    res = await spawner.spawn(
        AgentSpec(name="ro", description="只读", system_prompt="只读",
                  tools=["echo"], disallowed_tools=["write"]),
        "try write", base_registry=_registry(), base_model=model,
    )
    assert res.text == "recovered"
    tr = next(e for e in res.events if e.type == "tool_result")
    assert tr.tool_result is not None and not tr.tool_result.ok
    assert "unknown tool" in (tr.tool_result.error or "")


async def test_explore_rejects_write():
    spawner = SubagentSpawner(_settings())
    model = FakeModel([
        Decision(tool_calls=[ToolCall(id="c1", name="write", arguments={"path": "x"})]),
        Decision(text="recovered"),
    ])
    res = await spawner.spawn(
        BUILTIN_EXPLORE, "explore", base_registry=_registry(), base_model=model
    )
    tr = next(e for e in res.events if e.type == "tool_result")
    assert not tr.tool_result.ok


# --------------------------------------------------------------------------- #
# 模型降级
# --------------------------------------------------------------------------- #
def test_resolve_model_inherits_when_no_override():
    spawner = SubagentSpawner(_settings())
    fake = FakeModel([Decision(text="x")])
    assert spawner._resolve_model(fake, AgentSpec(name="a", description="", system_prompt="")) is fake


def test_resolve_model_overrides_with_spec_model():
    settings = _settings()
    settings.llm.api_key = "sk-test"
    spawner = SubagentSpawner(settings)
    m = spawner._resolve_model(
        None, AgentSpec(name="a", description="", system_prompt="", model="deepseek-chat")
    )
    from agent.core.model import OpenAICompatibleModel
    assert isinstance(m, OpenAICompatibleModel)
    assert m.model == "deepseek-chat"


# --------------------------------------------------------------------------- #
# 深度限制
# --------------------------------------------------------------------------- #
async def test_depth_limit_raises():
    spawner = SubagentSpawner(_settings(), max_depth=3)
    model = FakeModel([Decision(text="x")])

    async def rec(depth):
        await spawner.spawn(
            BUILTIN_GENERAL, "t", base_registry=_registry(), base_model=model, depth=depth
        )
        await rec(depth + 1)

    with pytest.raises(RecursionError):
        await rec(0)


# --------------------------------------------------------------------------- #
# Trace 父子
# --------------------------------------------------------------------------- #
async def test_trace_parent_links_child_span():
    tracer = Tracer()
    spawner = SubagentSpawner(_settings(), tracer=tracer)
    model = FakeModel([Decision(text="x")])
    with tracer.span("parent.work", kind="agent") as parent:
        await spawner.spawn(
            BUILTIN_GENERAL, "sub", base_registry=_registry(), base_model=model,
            parent_span=parent,
        )
    # 子 agent.run 的 parent_id == parent.id
    child_root = next(s for s in tracer.spans if s.name == "agent.run")
    assert child_root.parent_id == parent.id
    # 子 model.act 的父是子 agent.run（间接挂到 parent 下）
    model_spans = [s for s in tracer.spans if s.name == "model.act"]
    assert model_spans and model_spans[0].parent_id == child_root.id


async def test_trace_parent_parallel_safe():
    tracer = Tracer()
    spawner = SubagentSpawner(_settings(), tracer=tracer)
    model = FakeModel([Decision(text="x")])
    with tracer.span("parent.work", kind="agent") as parent:
        # 每个子 agent 用独立 model 实例，避免共享脚本被并发耗尽
        results = await asyncio.gather(
            asyncio.create_task(spawner.spawn(
                BUILTIN_GENERAL, "a", base_registry=_registry(),
                base_model=FakeModel([Decision(text="x")]),
                parent_span=parent)),
            asyncio.create_task(spawner.spawn(
                BUILTIN_GENERAL, "b", base_registry=_registry(),
                base_model=FakeModel([Decision(text="x")]),
                parent_span=parent)),
        )
    assert all(r.text == "x" for r in results)
    child_roots = [s for s in tracer.spans if s.name == "agent.run"]
    assert len(child_roots) == 2
    assert all(c.parent_id == parent.id for c in child_roots)


# --------------------------------------------------------------------------- #
# _SubAgentTransport：渲染 + 屏蔽 HITL
# --------------------------------------------------------------------------- #
def test_sub_transport_is_agent_transport_and_delegates_interactive():
    parent = _FakeParentTransport(interactive=True)
    sub = _SubAgentTransport(parent=parent, name="explore")
    assert isinstance(sub, AgentTransport)
    assert sub.interactive is True

    # 非交互 parent → 子 agent 也非交互
    sub2 = _SubAgentTransport(parent=_FakeParentTransport(interactive=False))
    assert sub2.interactive is False


async def test_sub_transport_ask_delegates_to_parent():
    parent = _FakeParentTransport(interactive=True)
    sub = _SubAgentTransport(parent=parent)
    from agent.core.intent import Question
    q = Question(question="clarify?")
    ans = await sub.ask(q)
    assert ans == "answered"
    assert parent.asked == [q]


async def test_sub_transport_ask_raises_without_interactive_parent():
    sub = _SubAgentTransport(parent=_FakeParentTransport(interactive=False))
    from agent.core.intent import Question
    with pytest.raises(RuntimeError):
        await sub.ask(Question(question="x"))


async def test_parent_eventstream_not_polluted_by_subagent():
    parent = _FakeParentTransport(interactive=True)
    spawner = SubagentSpawner(_settings())
    model = FakeModel([Decision(text="sub-done")])
    await spawner.spawn(
        BUILTIN_GENERAL, "sub", base_registry=_registry(), base_model=model,
        parent_transport=parent,
    )
    # 子 agent 拥有独立 EventStream；事件经 _SubAgentTransport 渲染，不混入父传输的事件流
    assert parent.rendered == []


async def test_sub_transport_renders_independent_stream():
    """_SubAgentTransport.bind 订阅的是子 agent 的独立 stream（非父 stream）。"""
    parent = _FakeParentTransport(interactive=True)
    sub = _SubAgentTransport(parent=parent, name="explore")
    child_stream = EventStream()
    sub.bind(child_stream)
    # 往子 stream 投递事件，应被 sub 渲染（父 transport 不应收到）
    from agent.core.events import Event
    child_stream.append(Event(type="text", text="hello", kind="content"))
    assert len(parent.rendered) == 0


def test_sub_transport_panel_height_default():
    """_SubAgentTransport 默认 panel_height=15。"""
    parent = _FakeParentTransport(interactive=True)
    sub = _SubAgentTransport(parent=parent, name="explore")
    assert sub._panel_height == 15


def test_sub_transport_panel_height_custom():
    """_SubAgentTransport 支持自定义 panel_height。"""
    parent = _FakeParentTransport(interactive=True)
    sub = _SubAgentTransport(parent=parent, name="explore", panel_height=5)
    assert sub._panel_height == 5


def test_sub_transport_panel_height_at_least_1():
    """panel_height 至少为 1。"""
    parent = _FakeParentTransport(interactive=True)
    sub = _SubAgentTransport(parent=parent, name="explore", panel_height=0)
    assert sub._panel_height == 1
    sub2 = _SubAgentTransport(parent=parent, name="explore", panel_height=-5)
    assert sub2._panel_height == 1


def test_sub_transport_non_interactive_skips_live():
    """非交互模式下不启动 Live（不抛错）。"""
    parent = _FakeParentTransport(interactive=False)
    sub = _SubAgentTransport(parent=parent, name="explore", panel_height=10)
    assert sub._sub_live is None
    # bind 不应抛错
    sub.bind(EventStream())
    sub.close()
