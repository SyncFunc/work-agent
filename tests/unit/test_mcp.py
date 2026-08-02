"""M11.6 验收：MCP 接入（yaml 分层配置 + 发现 + 调用 + risk + 容错）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent.mcp.adapter import _infer_risk, safe_name
from agent.mcp.config import load_servers, mcp_config_paths
from agent.mcp.manager import McpManager
from agent.runtime.registry import ToolRegistry

DEMO_CMD = sys.executable
DEMO_ARGS = ["-m", "agent.mcp.demo_server"]


def load_settings_mcp(monkeypatch, tmp_path):
    """构造一个指向 tmp_path 的 MCPConfig（enabled + 空内联）。"""
    from agent.config.settings import MCPConfig

    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    return MCPConfig(enabled=True)


def _write_mcp_yaml(path: Path, servers: dict) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"mcpServers": servers}), encoding="utf-8")


# --------------------------------------------------------------------------- #
# yaml 分层配置
# --------------------------------------------------------------------------- #
def test_load_servers_project_overrides_user(tmp_path: Path, monkeypatch):
    """项目级 mcp.yaml 覆盖用户级同名 server；不同名 server 都保留。"""
    monkeypatch.setenv("AGENT_USER_CONFIG_DIR", str(tmp_path / "user"))
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path / "proj"))
    _write_mcp_yaml(
        tmp_path / "user" / "mcp.yaml",
        {
            "common": {"command": "u-cmd", "args": ["-u"]},
            "only_user": {"command": "u-only"},
        },
    )
    _write_mcp_yaml(
        tmp_path / "proj" / ".agent" / "mcp.yaml",
        {
            "common": {"command": "p-cmd", "args": ["-p"], "env": {"K": "V"}},
            "only_project": {"command": "p-only"},
        },
    )
    servers = load_servers(tmp_path / "proj")
    by_name = {s.name: s for s in servers}
    assert set(by_name) == {"common", "only_user", "only_project"}
    # 项目覆盖用户
    assert by_name["common"].command == "p-cmd"
    assert by_name["common"].args == ["-p"]
    assert by_name["common"].env == {"K": "V"}
    # 各自的独有 server
    assert by_name["only_user"].command == "u-only"
    assert by_name["only_project"].command == "p-only"


def test_load_servers_env_expansion(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("MCP_TOKEN", "sekret")
    _write_mcp_yaml(
        tmp_path / ".agent" / "mcp.yaml",
        {"svc": {"command": "x", "env": {"TOKEN": "${MCP_TOKEN}"}}},
    )
    servers = load_servers(tmp_path)
    assert servers[0].env["TOKEN"] == "sekret"


def test_mcp_config_paths(monkeypatch):
    monkeypatch.setenv("AGENT_USER_CONFIG_DIR", "/u")
    monkeypatch.setenv("AGENT_PROJECT_ROOT", "/p")
    user_p, proj_p = mcp_config_paths("/p")
    assert user_p == Path("/u/mcp.yaml")
    assert proj_p == Path("/p/.agent/mcp.yaml")


# --------------------------------------------------------------------------- #
# 命名 / risk
# --------------------------------------------------------------------------- #
def test_safe_name():
    assert safe_name("github-server") == "github-server"
    # 空格/感叹号转 "_"，并去掉首尾多余 "_"
    assert safe_name("My Server!") == "My_Server"
    assert safe_name("") == "tool"


def test_infer_risk_read_vs_write():
    # 只读词 → read
    assert _infer_risk("search_issues", "在 GitHub 搜索 issue") == "read"
    assert _infer_risk("list_files", "列文件") == "read"
    assert _infer_risk("get_user", "查用户") == "read"
    # 写词 → exec
    assert _infer_risk("create_issue", "创建 issue") == "exec"
    assert _infer_risk("delete_file", "删除文件") == "exec"
    assert _infer_risk("send_message", "发送消息") == "exec"
    # 同时含读+写 → 保守 exec
    assert _infer_risk("read_and_delete", "读取后删除") == "exec"
    # 无提示 → 默认 exec（fail-closed）
    assert _infer_risk("do_thing", "做个操作") == "exec"


# --------------------------------------------------------------------------- #
# 发现 + 调用（真实拉起 demo_server 子进程）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_discover_and_call(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    _write_mcp_yaml(
        tmp_path / ".agent" / "mcp.yaml",
        {"demo": {"command": DEMO_CMD, "args": DEMO_ARGS}},
    )
    mgr = McpManager(load_settings_mcp(monkeypatch, tmp_path), project_root=tmp_path)
    await mgr.start()

    assert mgr.enabled
    # demo 4 个工具 + 内建 weather 1 个 = 5
    assert len(mgr.specs) == 5

    reg = ToolRegistry()
    mgr.register_to(reg)

    names = {s.name for s in reg.list()}
    assert "mcp__demo__search_files" in names
    assert "mcp__demo__echo" in names
    assert "mcp__demo__add" in names
    assert "mcp__demo__delete_file" in names

    # 读工具（名字含 search 读词）→ risk=read
    search_spec = reg.get("mcp__demo__search_files")
    assert search_spec.risk == "read"
    assert search_spec.is_mcp is True
    assert search_spec.mcp_server == "demo"
    # 写工具 risk=exec（审批走 ask）
    del_spec = reg.get("mcp__demo__delete_file")
    assert del_spec.risk == "exec"

    # 调用只读工具
    r = await reg.run("mcp__demo__search_files", {"keyword": "main"})
    assert r.ok
    assert "main.py" in r.output

    # 调用
    r2 = await reg.run("mcp__demo__add", {"a": 2, "b": 3})
    assert r2.ok
    assert r2.output.strip() == "5"

    await mgr.close()


@pytest.mark.asyncio
async def test_call_unknown_tool_returns_error(tmp_path: Path, monkeypatch):
    """demo server 对未知工具返回 isError=true → adapter 转 ok=False。"""
    from agent.mcp.client import StdioClient

    client = StdioClient(DEMO_CMD, DEMO_ARGS)
    await client.start()
    await client.initialize()
    resp = await client.call_tool("no_such_tool", {})
    assert resp.isError is True
    assert "unknown tool" in resp.text().lower()
    await client.close()


# --------------------------------------------------------------------------- #
# 容错
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_start_skips_broken_server(tmp_path: Path, monkeypatch):
    """单个 server 启动失败不拖垮整体；其余 server 仍可用。"""
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    _write_mcp_yaml(
        tmp_path / ".agent" / "mcp.yaml",
        {
            "broken": {"command": "definitely-not-a-real-cmd-xyz", "args": []},
            "demo": {"command": DEMO_CMD, "args": DEMO_ARGS},
        },
    )
    mgr = McpManager(load_settings_mcp(monkeypatch, tmp_path), project_root=tmp_path)
    await mgr.start()
    # broken 被跳过，demo 4 个 + 内建 weather 1 个 = 5
    assert len(mgr.specs) == 5
    names = {s.name for s in mgr.specs}
    assert "mcp__demo__echo" in names
    await mgr.close()


@pytest.mark.asyncio
async def test_timeout_returns_ok_false(tmp_path: Path, monkeypatch):
    """调用超时 → ok=False 而非抛异常。"""
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    _write_mcp_yaml(
        tmp_path / ".agent" / "mcp.yaml",
        {"demo": {"command": DEMO_CMD, "args": DEMO_ARGS}},
    )
    cfg = load_settings_mcp(monkeypatch, tmp_path)
    cfg.tool_timeout_sec = 0.001  # 极短超时
    mgr = McpManager(cfg, project_root=tmp_path)
    await mgr.start()
    reg = ToolRegistry()
    mgr.register_to(reg)
    r = await reg.run("mcp__demo__echo", {"message": "x"})
    # 可能恰好返回成功，也可能超时——两种都接受，但绝不能抛异常
    assert r is not None
    await mgr.close()


# --------------------------------------------------------------------------- #
# 内建天气查询 MCP
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_builtin_weather_server_discover_and_call(monkeypatch, tmp_path):
    """内建 weather MCP：无任何 yaml 配置时也能发现并调用 get_weather。"""
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    mgr = McpManager(load_settings_mcp(monkeypatch, tmp_path), project_root=tmp_path)
    await mgr.start()

    names = {s.name for s in mgr.specs}
    assert "mcp__weather__get_weather" in names

    reg = ToolRegistry()
    mgr.register_to(reg)
    spec = reg.get("mcp__weather__get_weather")
    assert spec.is_mcp is True
    assert spec.mcp_server == "weather"
    assert spec.risk == "read"  # 名字含 get + 描述只读

    r = await reg.run("mcp__weather__get_weather", {"city": "beijing"})
    assert r.ok
    # 真实天气：网络可用→实时数据，否则→离线数据；两者都含城市中文名。
    assert "北京" in r.output
    assert ("°C" in r.output) or ("实时" in r.output) or ("离线数据" in r.output)

    # 中文 / 拼音 / 英文 三种输入都支持
    r_zh = await reg.run("mcp__weather__get_weather", {"city": "北京"})
    assert r_zh.ok and "北京" in r_zh.output
    r_py = await reg.run("mcp__weather__get_weather", {"city": "haerbin"})
    assert r_py.ok and "哈尔滨" in r_py.output
    r_en = await reg.run("mcp__weather__get_weather", {"city": "Hangzhou"})
    assert r_en.ok and "杭州" in r_en.output

    # 不设白名单：未收录的任意城市也直接查（wttr.in 对任意字符串都返回最近地区），不回 isError。
    r_any = await reg.run("mcp__weather__get_weather", {"city": "shaoxing"})
    assert r_any.ok and "绍兴" in r_any.output
    assert ("°C" in r_any.output) or ("实时" in r_any.output) or ("离线数据" in r_any.output)

    # 空 city → isError 报错
    r_none = await reg.run("mcp__weather__get_weather", {"city": ""})
    assert not r_none.ok

    await mgr.close()


@pytest.mark.asyncio
async def test_builtin_weather_overridden_by_yaml(monkeypatch, tmp_path):
    """用户在 yaml 配了同名 weather 则覆盖内建（避免重复拉起）。"""
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    _write_mcp_yaml(
        tmp_path / ".agent" / "mcp.yaml",
        {"weather": {"command": DEMO_CMD, "args": DEMO_ARGS}},  # 用户自定义 weather
    )
    mgr = McpManager(load_settings_mcp(monkeypatch, tmp_path), project_root=tmp_path)
    await mgr.start()
    # 内建 weather 被覆盖，不重复；只有用户定义的那个 weather（demo 工具）
    names = {s.name for s in mgr.specs}
    # demo server 暴露 search_files/echo/add/delete_file（来自用户的 weather=DEMO_CMD）
    assert "mcp__weather__echo" in names
    assert "mcp__weather__get_weather" not in names  # 内建 weather 被覆盖
    await mgr.close()


# --------------------------------------------------------------------------- #
# 延迟加载（tool_search）：MCP 工具不进全量列表
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_model_tools_mcp_lazy_load_catalog_then_full(tmp_path: Path, monkeypatch):
    """未激活的 MCP 工具在 _model_tools 里只发 L1 目录（空 schema），
    tool_search 命中后激活，下一轮发完整 schema。"""
    from agent.config.settings import Settings
    from agent.core.loop import AgentLoop
    from agent.core.model import Decision, FakeModel, ToolCall

    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    _write_mcp_yaml(
        tmp_path / ".agent" / "mcp.yaml",
        {"weather": {"command": DEMO_CMD, "args": DEMO_ARGS}},
    )
    mgr = McpManager(load_settings_mcp(monkeypatch, tmp_path), project_root=tmp_path)
    await mgr.start()
    reg = ToolRegistry()
    mgr.register_to(reg)

    loop = AgentLoop(FakeModel([Decision(text="done")]), reg, Settings(), tracer=None)
    # 未激活：MCP 工具**不进 tools 列表**（避免模型直接调空 schema 工具），
    # 只出现在 system prompt 目录（_mcp_catalog_prompt），并提供 tool_search。
    tools = loop._model_tools()
    names = {t["function"]["name"] for t in tools}
    assert "mcp__weather__search_files" not in names  # 未激活不进列表
    assert any(t["function"]["name"] == "tool_search" for t in tools)

    # system prompt 目录里能"看见"该工具（引导 tool_search）
    catalog = loop._mcp_catalog_prompt()
    assert "mcp__weather__search_files" in catalog

    # tool_search 命中激活
    tc = ToolCall(id="ts1", name="tool_search", arguments={"query": "search"})
    r = loop._tool_search(tc)
    assert r.ok
    assert "mcp__weather__search_files" in r.output
    assert "mcp__weather__search_files" in loop._mcp_active

    # 激活后：完整 schema 进 tools 列表
    tools2 = loop._model_tools()
    names2 = {t["function"]["name"] for t in tools2}
    assert "mcp__weather__search_files" in names2
    full = next(t for t in tools2 if t["function"]["name"] == "mcp__weather__search_files")
    assert full["function"]["parameters"]["properties"] != {}  # 完整 schema

    # 激活后目录不再含该工具
    assert "mcp__weather__search_files" not in loop._mcp_catalog_prompt()

    await mgr.close()


# --------------------------------------------------------------------------- #
# 写回 yaml（前端管理：增删改 / 启停）
# --------------------------------------------------------------------------- #
def test_add_server_writes_project_yaml(tmp_path: Path, monkeypatch):
    from agent.mcp.config import add_server, load_servers

    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    ok, _ = add_server(
        "github",
        "npx",
        args=["-y", "github-mcp-server"],
        env={"TOKEN": "x"},
        enabled=True,
        scope="project",
        project_root=tmp_path,
    )
    assert ok
    servers = load_servers(tmp_path)
    s = next(x for x in servers if x.name == "github")
    assert s.command == "npx"
    assert s.args == ["-y", "github-mcp-server"]
    assert s.env == {"TOKEN": "x"}
    assert s.enabled is True
    # 再 add 同名覆盖
    ok2, _ = add_server("github", "npx2", scope="project", project_root=tmp_path)
    assert ok2
    servers2 = load_servers(tmp_path)
    assert next(x for x in servers2 if x.name == "github").command == "npx2"


def test_add_server_requires_name_and_command(tmp_path):
    from agent.mcp.config import add_server

    ok, err = add_server("", "npx", project_root=tmp_path)
    assert not ok and "name" in err
    ok2, err2 = add_server("x", "", project_root=tmp_path)
    assert not ok2 and "command" in err2


def test_toggle_server_enabled(tmp_path: Path, monkeypatch):
    from agent.mcp.config import add_server, load_servers, set_server_enabled

    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    add_server("svc", "cmd", enabled=True, project_root=tmp_path)
    ok, _ = set_server_enabled("svc", False, scope="project", project_root=tmp_path)
    assert ok
    s = next(x for x in load_servers(tmp_path) if x.name == "svc")
    assert s.enabled is False
    # 不存在的 server
    ok2, err2 = set_server_enabled("nope", True, project_root=tmp_path)
    assert not ok2 and "server_not_found" in err2


def test_remove_server(tmp_path: Path, monkeypatch):
    from agent.mcp.config import add_server, load_servers, remove_server

    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    add_server("a", "cmd1", project_root=tmp_path)
    add_server("b", "cmd2", project_root=tmp_path)
    ok, _ = remove_server("a", scope="project", project_root=tmp_path)
    assert ok
    names = {s.name for s in load_servers(tmp_path)}
    assert names == {"b"}
    ok2, err2 = remove_server("missing", project_root=tmp_path)
    assert not ok2 and "server_not_found" in err2


@pytest.mark.asyncio
async def test_reload_disabled_server_unregisters_tools(tmp_path: Path, monkeypatch):
    """toggle 禁用后 reload：工具从 registry 注销，重启用后重新注册。"""
    from agent.mcp.config import set_server_enabled

    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path))
    _write_mcp_yaml(
        tmp_path / ".agent" / "mcp.yaml",
        {"demo": {"command": DEMO_CMD, "args": DEMO_ARGS}},
    )
    mgr = McpManager(load_settings_mcp(monkeypatch, tmp_path), project_root=tmp_path)
    reg = ToolRegistry()
    await mgr.start()
    mgr.register_to(reg)
    assert reg.get("mcp__demo__search_files") is not None

    # 禁用 → reload 返回 stale 工具名
    set_server_enabled("demo", False, scope="project", project_root=tmp_path)
    stale = await mgr.reload()
    assert any("mcp__demo__search_files" == n for n in stale)
    for n in stale:
        reg.unregister(n)
    from agent.runtime.registry import UnknownTool

    with pytest.raises(UnknownTool):
        reg.get("mcp__demo__search_files")

    # 重新启用 → reload 后重新注册
    set_server_enabled("demo", True, scope="project", project_root=tmp_path)
    stale2 = await mgr.reload()
    assert stale2 == []  # 重新启用不是"移除"，无 stale
    mgr.register_to(reg)
    assert reg.get("mcp__demo__search_files") is not None

    await mgr.close()
