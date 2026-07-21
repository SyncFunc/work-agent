"""M1.3 验收：ReAct 循环 + 事件流。

覆盖：基础流程、同轮并发、卡死检测、max_iterations 上限、UnknownTool 不崩、事件序列化。
全程用 FakeModel / RecordingModel，不依赖真实 LLM 与真实工具副作用。
"""

import asyncio
from pathlib import Path

import pytest

from agent.config.settings import Settings
from agent.core.control_tools import SPAWN_SUBAGENT_TOOL_NAME, USE_SKILL_TOOL_NAME
from agent.core.events import EventStream
from agent.core.loop import AgentLoop, LoopStalled
from agent.core.model import Decision, FakeModel, RecordingModel, ToolCall
from agent.runtime.approval import Action, ApprovalGate
from agent.runtime.registry import ToolRegistry, ToolResult, tool
from agent.runtime.sandbox import FakeExecutor, SandboxProfile
from agent.skills.loader import SkillLoader
from agent.subagent import SubagentSpawner


# --------------------------------------------------------------------------- #
# 测试夹具
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
    loop = dict(max_iterations=20, max_tool_concurrency=5, max_repeat_calls=3)
    loop.update(kw.pop("loop", {}))
    for k in ("max_iterations", "max_tool_concurrency", "max_repeat_calls", "max_tool_output_chars"):
        if k in kw:
            loop[k] = kw.pop(k)
    return Settings(loop=loop, **kw)


# --------------------------------------------------------------------------- #
# 基础循环用例
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
    assert types == ["decision", "tool_use", "tool_result", "text", "decision", "final"]

    text_ev = next(e for e in result.events if e.type == "text")
    assert text_ev.text == "final answer"

    res_ev = next(e for e in result.events if e.type == "tool_result")
    assert res_ev.tool_result is not None and res_ev.tool_result.ok
    assert res_ev.tool_call_id == "c1"


async def test_concurrent_tools_run_in_parallel_and_pair_by_id():
    registry = _make_registry()
    calls = [
        ToolCall(id="c1", name="slow", arguments={"dt": 0.1, "tag": "A"}),
        ToolCall(id="c2", name="slow", arguments={"dt": 0.1, "tag": "B"}),
    ]
    model = FakeModel([Decision(tool_calls=calls), Decision(text="done")])
    loop = AgentLoop(model, registry, _settings(max_tool_concurrency=5))

    async def run():
        return await loop.run("concurrent")

    res = await asyncio.wait_for(run(), timeout=1.0)
    assert res.text == "done"

    tr = {e.tool_call_id: e.tool_result for e in res.events if e.type == "tool_result"}
    assert tr["c1"].output == "A"
    assert tr["c2"].output == "B"

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
    model = RecordingModel(decision=Decision(tool_calls=calls))
    loop = AgentLoop(model, registry, _settings(max_repeat_calls=3, max_tool_concurrency=5))

    with pytest.raises(LoopStalled):
        await loop.run("loop forever")

    assert counter["n"] == 4


async def test_max_iterations_soft_limit_returns_result():
    registry = _make_registry()
    calls = [ToolCall(id="c1", name="echo", arguments={"x": 2})]
    model = RecordingModel(decision=Decision(tool_calls=calls))
    loop = AgentLoop(model, registry, _settings(max_iterations=5, max_repeat_calls=1000))

    result = await loop.run("never ends")

    assert result.soft_limit_hit is True
    assert result.text and "最大轮次" in result.text
    assert result.iterations == 5
    assert result.messages is not None
    last = result.messages[-1]
    assert last.role == "tool"
    cont = await loop.run("继续", result.messages)
    assert cont.messages is not None
    assert cont.soft_limit_hit is True


async def test_unknown_tool_does_not_crash_loop():
    registry = _make_registry()
    model = FakeModel(
        [
            Decision(tool_calls=[ToolCall(id="c1", name="ghost", arguments={})]),
            Decision(text="recovered"),
        ]
    )
    loop = AgentLoop(model, registry, _settings())

    result = await loop.run("call unknown")
    assert result.text == "recovered"

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
    first_decision = next(e for e in rebuilt if e.type == "decision")
    assert first_decision.decision is not None
    assert first_decision.decision.tool_calls[0].name == "echo"
    assert first_decision.decision.tool_calls[0].arguments == {"x": 1}
    assert [e.seq for e in rebuilt] == list(range(len(rebuilt)))


async def test_usage_accumulates_across_iterations():
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

    assert transport.texts == [("final answer", "content")]
    assert transport.calls == ["echo"]
    assert transport.results == [("c1", True)]
    assert result.usage == {}


async def test_empty_name_toolcall_treated_as_final():
    registry = _make_registry()
    noise = Decision(
        text="这是审核计划",
        tool_calls=[ToolCall(id="n1", name="", arguments={})],
    )
    model = FakeModel([noise, noise, noise, noise, noise])
    loop = AgentLoop(model, registry, _settings(max_iterations=20))
    result = await loop.run("review loop.py")

    assert result.text == "这是审核计划"
    assert result.iterations == 1


async def test_tool_call_delta_streamed_to_transport():
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
    noise_ws = Decision(text="x", tool_calls=[ToolCall(id="w", name=" ", arguments={})])
    model2 = FakeModel([noise_ws])
    result2 = await AgentLoop(model2, _make_registry(), _settings()).run("t")
    assert result2.iterations == 1
    assert result2.text == "x"


# --------------------------------------------------------------------------- #
# M2 集成：bash 经沙箱执行 + 审批门 + 提权
# --------------------------------------------------------------------------- #
async def _noop_bash(args: dict) -> ToolResult:
    return ToolResult(ok=True, output="")


class _FakeTransport:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.last: "Action | None" = None

    @property
    def interactive(self) -> bool:
        return True

    async def approve(self, action: Action) -> bool:
        self.last = action
        return self.answer

    def bind(self, stream) -> None:
        pass


def _bash_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(tool("bash", risk="exec")(_noop_bash))
    return r


async def test_gate_none_runs_bash_via_fake_executor():
    """gate=None 时退化 M1：bash 仍经注入的 FakeExecutor 执行。"""
    fake = FakeExecutor()
    loop = AgentLoop(
        FakeModel([
            Decision(tool_calls=[ToolCall(id="b1", name="bash", arguments={"cmd": "echo hi"})]),
            Decision(text="done"),
        ]),
        _bash_registry(), _settings(), sandbox=fake,
    )
    res = await loop.run("task")
    assert res.text == "done"
    assert fake.requests and fake.requests[0].cmd == "echo hi"
    assert fake.requests[0].profile == SandboxProfile.WORKSPACE_WRITE


async def test_elevation_runs_at_elevated_profile():
    """联网命令经 ASK→批准后，自动以 danger-full 临时执行（不再需要 enable_elevation 开关）。"""
    fake = FakeExecutor()
    gate = ApprovalGate(
        "on-request",
        sandbox_profile=SandboxProfile.WORKSPACE_WRITE,
        elevated_profile=SandboxProfile.DANGER_FULL,
    )
    loop = AgentLoop(
        FakeModel([
            Decision(tool_calls=[ToolCall(id="b1", name="bash", arguments={"cmd": "npm install x"})]),
            Decision(text="done"),
        ]),
        _bash_registry(), _settings(), sandbox=fake, gate=gate,
    )
    # npm install 不带 approval_request → on-request 下 ALLOW → 不提权
    res = await loop.run("task")
    assert res.text == "done"
    assert fake.requests[0].profile == SandboxProfile.WORKSPACE_WRITE


async def test_elevation_on_approval_request():
    """模型显式 _approval_request → on-request 下走 ASK → 批准 → 自动提权。"""
    fake = FakeExecutor()
    gate = ApprovalGate(
        "on-request",
        sandbox_profile=SandboxProfile.WORKSPACE_WRITE,
        elevated_profile=SandboxProfile.DANGER_FULL,
    )
    loop = AgentLoop(
        FakeModel([
            Decision(tool_calls=[ToolCall(
                id="b1", name="bash", arguments={"cmd": "curl https://example.com"},
                approval_request=True,
            )]),
            Decision(text="done"),
        ]),
        _bash_registry(), _settings(), sandbox=fake, gate=gate,
    )
    await loop.run("task")
    assert fake.requests[0].profile == SandboxProfile.DANGER_FULL


async def test_no_elevation_on_allow():
    """普通 ALLOW 不提权：模型未请求且 on-request 下默认放行，仍以原 profile 执行。"""
    fake = FakeExecutor()
    gate = ApprovalGate("on-request", sandbox_profile=SandboxProfile.WORKSPACE_WRITE)
    loop = AgentLoop(
        FakeModel([
            Decision(tool_calls=[ToolCall(
                id="b1", name="bash", arguments={"cmd": "curl https://example.com"}
            )]),
            Decision(text="done"),
        ]),
        _bash_registry(), _settings(), sandbox=fake, gate=gate,
    )
    await loop.run("task")
    assert fake.requests[0].profile == SandboxProfile.WORKSPACE_WRITE


async def test_unless_trused_asks_and_rejects():
    """UNLESS_TRUSTED 模式 + 用户拒绝 → bash 被拦，不真执行。"""
    fake = FakeExecutor()
    gate = ApprovalGate("unless-trusted")
    transport = _FakeTransport(False)
    loop = AgentLoop(
        FakeModel([
            Decision(tool_calls=[ToolCall(id="b1", name="bash", arguments={"cmd": "ls"})]),
            Decision(text="done"),
        ]),
        _bash_registry(), _settings(), sandbox=fake, gate=gate,
    )
    res = await loop.run("task", transport=transport)
    tr = next(e for e in res.events if e.type == "tool_result")
    assert not tr.tool_result.ok
    assert "rejected by user approval" in (tr.tool_result.error or "")
    assert fake.requests == []


async def test_unless_trused_exec_policy_skips_ask():
    """UNLESS_TRUSTED 模式 + exec_policy 命中 → 免审直接执行。"""
    fake = FakeExecutor()
    gate = ApprovalGate("unless-trusted", exec_policy=["ls "])
    transport = _FakeTransport(False)
    loop = AgentLoop(
        FakeModel([
            Decision(tool_calls=[ToolCall(id="b1", name="bash", arguments={"cmd": "ls -la"})]),
            Decision(text="done"),
        ]),
        _bash_registry(), _settings(), sandbox=fake, gate=gate,
    )
    await loop.run("task", transport=transport)
    assert len(fake.requests) == 1  # 已执行，未被拦


# --------------------------------------------------------------------------- #
# M5.3 集成：SkillLoader + SubagentSpawner 接入 AgentLoop
# --------------------------------------------------------------------------- #
def _make_skill_dir(root: Path, name: str, body: str, **front: str) -> Path:
    d = root / ".agent" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    fm = "---\n" + "\n".join(f"{k}: {v}" for k, v in front.items()) + "\n---\n"
    (d / "SKILL.md").write_text(fm + body, encoding="utf-8")
    return d


async def test_system_prompt_includes_skills_catalog_not_body(tmp_path):
    _make_skill_dir(tmp_path, "demo", "SECRET BODY MUST NOT APPEAR IN SYSTEM PROMPT\n",
                    description="demo skill desc")
    loader = SkillLoader(tmp_path, user_root=tmp_path / "__u")
    loop = AgentLoop(FakeModel([Decision(text="x")]), _make_registry(), _settings(),
                     skill_loader=loader)
    sp = loop._system_prompt()
    assert "demo" in sp
    assert "demo skill desc" in sp
    assert "SECRET BODY" not in sp  # 双轨不变量：正文不进系统提示


async def test_system_prompt_lists_subagent_types():
    spawner = SubagentSpawner(_settings())
    loop = AgentLoop(FakeModel([Decision(text="x")]), _make_registry(), _settings(),
                     subagent_spawner=spawner)
    sp = loop._system_prompt()
    # 内置三种类型都应出现在触发目录（含 general-purpose）
    assert "explore" in sp
    assert "plan" in sp
    assert "general-purpose" in sp
    # 不变量：agent 的 system_prompt 正文不应灌进系统提示
    assert "你是通用执行 agent" not in sp


async def test_use_skill_injects_body_into_conv(tmp_path):
    _make_skill_dir(tmp_path, "demo", "DEMO BODY: do the thing\n",
                    description="demo skill")
    loader = SkillLoader(tmp_path, user_root=tmp_path / "__u")
    model = FakeModel([
        Decision(tool_calls=[ToolCall(id="s1", name=USE_SKILL_TOOL_NAME,
                                      arguments={"name": "demo"})]),
        Decision(text="done"),
    ])
    loop = AgentLoop(model, _make_registry(), _settings(), skill_loader=loader)
    res = await loop.run("task")
    assert res.text == "done"
    user_msgs = [m for m in res.messages if m.role == "user"]
    assert any("DEMO BODY" in (m.content or "") for m in user_msgs)
    tr = next(e for e in res.events if e.type == "tool_result")
    assert tr.tool_result is not None and tr.tool_result.ok


def test_use_skill_unknown_returns_error():
    loader = SkillLoader(Path("/nonexistent-project"), user_root=Path("/nonexistent-user"))
    loop = AgentLoop(FakeModel([Decision(text="x")]), _make_registry(), _settings(),
                     skill_loader=loader)
    res = loop._tool_use_skill(ToolCall(id="s1", name=USE_SKILL_TOOL_NAME,
                                        arguments={"name": "nope"}))
    assert not res.ok
    assert "unknown skill" in res.error


async def test_control_tools_present_when_enabled(tmp_path):
    loader = SkillLoader(tmp_path, user_root=tmp_path / "__u")
    spawner = SubagentSpawner(_settings())
    loop = AgentLoop(FakeModel([Decision(text="x")]), _make_registry(), _settings(),
                     skill_loader=loader, subagent_spawner=spawner)
    names = [t["function"]["name"] for t in loop._model_tools()]
    assert USE_SKILL_TOOL_NAME in names
    assert SPAWN_SUBAGENT_TOOL_NAME in names


async def test_control_tools_absent_when_disabled(tmp_path):
    s = _settings()
    s.skills.enabled = False
    s.subagents.enabled = False
    loader = SkillLoader(tmp_path, user_root=tmp_path / "__u")
    spawner = SubagentSpawner(_settings())
    loop = AgentLoop(FakeModel([Decision(text="x")]), _make_registry(), s,
                     skill_loader=loader, subagent_spawner=spawner)
    names = [t["function"]["name"] for t in loop._model_tools()]
    assert USE_SKILL_TOOL_NAME not in names
    assert SPAWN_SUBAGENT_TOOL_NAME not in names


async def test_spawn_subagent_returns_summary():
    spawner = SubagentSpawner(_settings())
    child_model = FakeModel([Decision(text="explore result")])
    loop = AgentLoop(child_model, _make_registry(), _settings(), subagent_spawner=spawner)
    tc = ToolCall(id="x", name=SPAWN_SUBAGENT_TOOL_NAME,
                  arguments={"agent": "explore", "task": "find stuff"})
    res = await loop._tool_spawn_subagent(tc)
    assert res.ok
    assert res.output.startswith("[Subagent explore]")
    assert "explore result" in res.output


async def test_spawn_subagent_depth_limit_returns_error():
    spawner = SubagentSpawner(_settings(), max_depth=1)
    loop = AgentLoop(FakeModel([Decision(text="x")]), _make_registry(), _settings(),
                     subagent_spawner=spawner)
    tc = ToolCall(id="x", name=SPAWN_SUBAGENT_TOOL_NAME,
                  arguments={"agent": "explore", "task": "t"})
    res = await loop._tool_spawn_subagent(tc)
    assert not res.ok
    assert "depth" in res.error.lower()


async def test_explore_subagent_runs_bash_without_approval():
    spawner = SubagentSpawner(_settings())
    child_model = FakeModel([
        Decision(tool_calls=[ToolCall(id="b1", name="bash", arguments={"cmd": "echo hi"})]),
        Decision(text="done"),
    ])
    loop = AgentLoop(child_model, _bash_registry(), _settings(), subagent_spawner=spawner)
    tc = ToolCall(id="x", name=SPAWN_SUBAGENT_TOOL_NAME,
                  arguments={"agent": "explore", "task": "ls"})
    res = await loop._tool_spawn_subagent(tc)
    assert res.ok
    assert "done" in res.output  # explore(gate=never) 不弹审批，直接执行并收尾


async def test_spawn_subagent_explore_rejects_write():
    spawner = SubagentSpawner(_settings())
    child_model = FakeModel([
        Decision(tool_calls=[ToolCall(id="w1", name="write",
                                      arguments={"path": "x", "content": "y"})]),
        Decision(text="recovered"),
    ])
    loop = AgentLoop(child_model, _make_registry(), _settings(), subagent_spawner=spawner)
    tc = ToolCall(id="x", name=SPAWN_SUBAGENT_TOOL_NAME,
                  arguments={"agent": "explore", "task": "t"})
    res = await loop._tool_spawn_subagent(tc)
    assert res.ok
    # explore 白名单拒 write → 子 agent 恢复并返回 "recovered"
    assert "recovered" in res.output


async def test_parent_eventstream_clean_after_spawn():
    """不变量：子 agent 内部事件不进主 EventStream（spawn 用独立 stream）。"""
    spawner = SubagentSpawner(_settings())
    child_model = FakeModel([Decision(text="sub done")])
    loop = AgentLoop(child_model, _make_registry(), _settings(), subagent_spawner=spawner)
    parent_stream = EventStream()
    tc = ToolCall(id="x", name=SPAWN_SUBAGENT_TOOL_NAME,
                  arguments={"agent": "explore", "task": "t"})
    res = await loop._tool_spawn_subagent(tc)
    assert res.ok
    # _tool_spawn_subagent 不向父 stream 写任何子内部事件
    assert len(parent_stream) == 0
