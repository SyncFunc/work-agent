"""M1.2 验收：ToolRegistry 注册、查询、调度；@tool 装饰器；未知工具报错。"""

from agent.runtime.registry import (
    ToolRegistry,
    ToolResult,
    ToolSpec,
    UnknownTool,
    tool,
)


async def test_register_and_get_returns_spec():
    reg = ToolRegistry()

    @tool("ping", risk="read")
    async def ping(args):
        return ToolResult(ok=True, output="pong")

    reg.register(ping)
    spec = reg.get("ping")
    assert isinstance(spec, ToolSpec)
    assert spec.name == "ping"
    assert spec.risk == "read"


async def test_get_unknown_raises():
    reg = ToolRegistry()
    try:
        reg.get("nope")
        raise AssertionError("expected UnknownTool")
    except UnknownTool:
        pass


async def test_list_returns_all_registered():
    reg = ToolRegistry()

    @tool("a", risk="read")
    async def a(_):
        return ToolResult(ok=True)

    @tool("b", risk="edit", schema={"type": "object", "description": "b"})
    async def b(_):
        return ToolResult(ok=True)

    reg.register(a)
    reg.register(b)
    names = {s.name for s in reg.list()}
    assert names == {"a", "b"}
    # schema 自描述：b 带自定义 schema
    bspec = next(s for s in reg.list() if s.name == "b")
    assert bspec.schema["description"] == "b"


async def test_run_dispatches_to_fn():
    reg = ToolRegistry()

    @tool("add", risk="exec")
    async def add(args):
        return ToolResult(ok=True, output=str(args["x"] + args["y"]))

    reg.register(add)
    r = await reg.run("add", {"x": 2, "y": 3})
    assert r.ok and r.output == "5"


async def test_run_unknown_raises():
    reg = ToolRegistry()
    try:
        await reg.run("missing", {})
        raise AssertionError("expected UnknownTool")
    except UnknownTool:
        pass


def test_invalid_risk_rejected():
    reg = ToolRegistry()
    try:
        reg.register(ToolSpec(name="x", fn=lambda a: None, risk="danger"))  # type: ignore[arg-type]
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_to_openai_tools_shape():
    reg = ToolRegistry()

    @tool(
        "grep",
        risk="read",
        schema={"type": "object", "description": "grep", "properties": {"pat": {"type": "string"}}},
    )
    async def grep(_):
        return ToolResult(ok=True)

    reg.register(grep)
    tools = reg.to_openai_tools()
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "grep"
    assert tools[0]["function"]["parameters"]["description"] == "grep"


async def test_run_truncates_output_over_limit():
    reg = ToolRegistry()

    @tool("big", risk="read")
    async def big(_):
        return ToolResult(ok=True, output="X" * 5000)

    reg.register(big)
    # 限制 100 字符
    r = await reg.run("big", {}, max_output_chars=100)
    assert r.ok
    assert len(r.output) == 100 + len("\n... [output truncated: 5000 chars, kept first 100]")
    assert "output truncated: 5000 chars" in r.output
    assert r.output.startswith("X" * 100)


async def test_run_no_truncation_when_under_limit():
    reg = ToolRegistry()

    @tool("small", risk="read")
    async def small(_):
        return ToolResult(ok=True, output="hello")

    reg.register(small)
    # 不传 max_output_chars（默认不限制）
    r = await reg.run("small", {})
    assert r.output == "hello"
    # 上限远大于输出也不截断
    r2 = await reg.run("small", {}, max_output_chars=1000)
    assert r2.output == "hello"
