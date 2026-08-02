"""MCP Server 配置加载：统一 yaml，分层 用户级 + 项目级。

约定（与 settings.yaml 完全对齐）：
  - 用户级：``~/.agent/mcp.yaml``（AGENT_USER_CONFIG_DIR 可覆盖，优先级低）
  - 项目级：``<project>/.agent/mcp.yaml``（AGENT_PROJECT_ROOT 可覆盖，优先级高）
  合并顺序：用户级先读，项目级后读 → 项目覆盖用户（同名 server 以项目为准）。

yaml 格式（对齐主流 mcpServers 约定）：
    mcpServers:
      github:
        command: npx
        args: ["-y", "github-mcp-server"]
        env:
          TOKEN: "${GITHUB_TOKEN}"
        cwd: /opt/x
      demo:
        command: python
        args: ["-m", "agent.mcp.demo_server"]

支持 ``${VAR}`` 环境变量展开（敏感凭据放环境变量，不进版本控制）。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_log = logging.getLogger(__name__)

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class McpServerConfig:
    """一个 MCP Server 的启动定义（stdio 本地子进程）。"""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    enabled: bool = True  # M11.6：决定是否拉起该 Server（前端可开关）


def _expand_env(value: str, fallback: str = "") -> str:
    """把 ``${VAR}`` 展开为环境变量；未设置时用 fallback（默认空）。"""

    def _sub(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), fallback)

    return _VAR_RE.sub(_sub, value)


def mcp_config_paths(project_root: str | Path | None = None) -> tuple[Path, Path]:
    """返回 (用户级, 项目级) 两个 mcp.yaml 路径。

    project_root 缺省时用 AGENT_PROJECT_ROOT，再退化为 Path.cwd()。
    """
    user_base = Path(os.environ.get("AGENT_USER_CONFIG_DIR") or Path.home() / ".agent")
    project_base = (
        Path(project_root)
        if project_root
        else Path(os.environ.get("AGENT_PROJECT_ROOT") or Path.cwd())
    )
    return user_base / "mcp.yaml", project_base / ".agent" / "mcp.yaml"


def builtin_weather_server() -> McpServerConfig:
    """内建天气查询 MCP Server（随代码分发，开箱即用）。

    供 McpManager 加载、daemon `show_mcp` 展示共用，避免两处各写一份导致面板漏显。
    """
    import sys

    return McpServerConfig(
        name="weather",
        command=sys.executable,
        args=["-m", "agent.mcp.weather_server"],
        enabled=True,
    )


def _load_server_dict(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        _log.warning("[mcp] failed to load %s: %s", path, e)
        return {}
    servers = (data or {}).get("mcpServers") or {}
    if not isinstance(servers, dict):
        return {}
    return {k: v for k, v in servers.items() if isinstance(v, dict)}


def load_servers(
    project_root: str | Path | None = None, inline: dict[str, Any] | None = None
) -> list[McpServerConfig]:
    """加载 MCP Server 清单：用户级 + 项目级 yaml 合并（项目覆盖用户）+ 内联覆盖。

    仅做配置解析，不拉起任何进程；供 McpManager 与 daemon 无会话查询共用。
    """
    merged: dict[str, dict[str, Any]] = {}
    user_path, project_path = mcp_config_paths(project_root)
    # 用户级（低优先级）
    merged.update(_load_server_dict(user_path))
    # 项目级（覆盖用户）
    merged.update(_load_server_dict(project_path))
    # 内联（最高优先级）
    for k, v in (inline or {}).items():
        if isinstance(v, dict):
            merged[k] = v

    servers: list[McpServerConfig] = []
    for name, raw in merged.items():
        command = raw.get("command")
        if not command or not isinstance(command, str):
            _log.warning("[mcp] server %r missing 'command', skip", name)
            continue
        args = [str(a) for a in (raw.get("args") or [])]
        env_raw = raw.get("env") or {}
        env = {str(k): _expand_env(str(v)) for k, v in env_raw.items()}
        cwd = raw.get("cwd")
        enabled = bool(raw.get("enabled", True))
        servers.append(
            McpServerConfig(
                name=name,
                command=command,
                args=args,
                env=env,
                cwd=str(cwd) if cwd else None,
                enabled=enabled,
            )
        )
    return servers


# --------------------------------------------------------------------------- #
# 写回 yaml（供前端管理：增删改 / 启停）
# --------------------------------------------------------------------------- #
def _scope_path(project_root: str | Path | None, scope: str) -> Path:
    """返回 scope 对应的 mcp.yaml 路径。scope ∈ {user, project}（默认 project）。"""
    user_path, project_path = mcp_config_paths(project_root)
    return project_path if scope == "project" else user_path


def _read_all(path: Path) -> dict[str, dict[str, Any]]:
    """读整个 mcp.yaml 的 mcpServers 原始 dict（不做 env 展开，保留原样）。"""
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        _log.warning("[mcp] failed to read %s: %s", path, e)
        return {}
    servers = (data or {}).get("mcpServers") or {}
    return (
        {k: v for k, v in servers.items() if isinstance(v, dict)}
        if isinstance(servers, dict)
        else {}
    )


def _write_all(path: Path, servers: dict[str, dict[str, Any]]) -> None:
    """把 mcpServers dict 原子写回 yaml（临时文件 + replace）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump({"mcpServers": servers}, allow_unicode=True, sort_keys=False)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)


def add_server(
    name: str,
    command: str,
    *,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    enabled: bool = True,
    scope: str = "project",
    project_root: str | Path | None = None,
) -> tuple[bool, str]:
    """新增/覆盖一个 MCP Server（写回 scope 层 yaml）。已存在则覆盖。"""
    if not name or not name.strip():
        return False, "name required"
    if not command:
        return False, "command required"
    path = _scope_path(project_root, scope)
    servers = _read_all(path)
    raw: dict[str, Any] = {"command": command}
    if args:
        raw["args"] = list(args)
    if env:
        raw["env"] = {k: v for k, v in env.items()}
    if cwd:
        raw["cwd"] = cwd
    raw["enabled"] = enabled
    servers[name.strip()] = raw
    try:
        _write_all(path, servers)
    except OSError as e:
        return False, f"write failed: {e}"
    return True, path.as_posix()


def remove_server(
    name: str, *, scope: str = "project", project_root: str | Path | None = None
) -> tuple[bool, str]:
    """从 scope 层 yaml 删除一个 MCP Server。"""
    if not name or not name.strip():
        return False, "name required"
    path = _scope_path(project_root, scope)
    servers = _read_all(path)
    if name.strip() not in servers:
        return False, "server_not_found"
    del servers[name.strip()]
    try:
        _write_all(path, servers)
    except OSError as e:
        return False, f"write failed: {e}"
    return True, path.as_posix()


def set_server_enabled(
    name: str, enabled: bool, *, scope: str = "project", project_root: str | Path | None = None
) -> tuple[bool, str]:
    """启停一个 MCP Server（写入 enabled 字段）。server 必须存在于 scope 层。"""
    if not name or not name.strip():
        return False, "name required"
    path = _scope_path(project_root, scope)
    servers = _read_all(path)
    if name.strip() not in servers:
        return False, "server_not_found"
    servers[name.strip()]["enabled"] = bool(enabled)
    try:
        _write_all(path, servers)
    except OSError as e:
        return False, f"write failed: {e}"
    return True, path.as_posix()
