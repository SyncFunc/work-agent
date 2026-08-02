"""MCPManager：MCP Server 生命周期 + 工具发现 + 注册进 ToolRegistry。

职责：
- 构造期（同步）：加载分层 mcp.yaml → Server 定义，准备好配置（不启动进程）。
- start()（async，幂等）：拉起各**启用**的 Server 子进程、initialize 握手、tools/list 发现工具，
  把每个 MCP 工具翻译成 ToolSpec，供 register_to 注册。
- reload()（async）：配置变更后重载——新启用/新增的拉起、改动的重连、禁用/删除的停掉，
  并返回「可移除工具列表」供调用方从 registry 注销。
- reload_if_changed()：按配置文件 mtime 惰性检测是否变化（loop 每轮 run 调用）。
- 容错：单个 Server 启动/握手失败只记日志跳过，不拖垮整体。
- 工具命名：mcp__<server>__<tool> 三段式 + 非法字符清洗（对齐 Claude Code）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from agent.config.settings import MCPConfig
from agent.mcp.adapter import McpToolAdapter
from agent.mcp.client import StdioClient
from agent.mcp.config import McpServerConfig, builtin_weather_server, load_servers, mcp_config_paths
from agent.runtime.registry import ToolRegistry, ToolSpec

_log = logging.getLogger(__name__)


class McpManager:
    """管理全部 MCP Server 的连接与工具注册。"""

    def __init__(self, settings: MCPConfig, project_root: Path | None = None) -> None:
        self.settings = settings
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self._load_config()
        self._clients: dict[str, StdioClient] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        # 每个 server 发现的工具（供重载时按 server 注销）
        self._server_specs: dict[str, list[ToolSpec]] = {}
        self._started = False

    # -- 配置 ------------------------------------------------------------ #
    def _load_config(self) -> None:
        servers = load_servers(self.project_root, inline=self.settings.servers)
        # 内建 MCP（天气查询）：开箱即用；若用户已在 yaml 配置了同名 weather 则覆盖。
        if not any(s.name == "weather" for s in servers):
            servers.append(builtin_weather_server())
        self._servers = servers
        self._running_configs: dict[str, McpServerConfig] = {
            s.name: s for s in self._servers if s.enabled
        }

    def _config_mtimes(self) -> dict[str, float]:
        user_p, proj_p = mcp_config_paths(self.project_root)
        mtimes: dict[str, float] = {}
        for p in (user_p, proj_p):
            try:
                mtimes[p.as_posix()] = p.stat().st_mtime if p.is_file() else 0.0
            except OSError:
                mtimes[p.as_posix()] = 0.0
        return mtimes

    @property
    def enabled(self) -> bool:
        return self.settings.enabled and bool(self._running_configs)

    @property
    def specs(self) -> list[ToolSpec]:
        """已发现的 MCP 工具（start() 后填充）。"""
        out: list[ToolSpec] = []
        for specs in self._server_specs.values():
            out.extend(specs)
        return out

    async def start(self) -> None:
        """幂等地拉起所有**启用**的 Server、握手、发现工具。"""
        if self._started or not self.settings.enabled:
            self._started = True
            return
        self._started = True
        for cfg in self._running_configs.values():
            try:
                await self._start_one(cfg)
            except Exception as e:  # noqa: BLE001 - 单个 server 失败不拖垮整体
                _log.warning("[mcp] server %r failed to start: %s", cfg.name, e)

    async def reload_if_changed(self) -> None:
        """配置文件 mtime 变化时重载（loop 每轮 run 可调用）。"""
        if not self._started or not self.settings.enabled:
            return
        if self._config_mtimes() != getattr(self, "_last_mtimes", None):
            await self.reload()

    async def reload(self) -> list[str]:
        """重载配置并按差异重连。返回「应注销的工具名」列表（供调用方 unregister）。

        变更类型：
        - 新增/启用的 server → 拉起
        - command/args/env 变化的已启用 server → 重连
        - 禁用/删除的 server → 关闭并从结果里带出需注销的工具
        """
        self._last_mtimes = self._config_mtimes()
        prev = dict(self._running_configs)
        self._load_config()
        next_configs = dict(self._running_configs)

        # 1) 被禁用/删除的：关闭连接，返回其工具名
        stale_specs: list[str] = []
        for name in list(prev):
            if name not in next_configs:
                stale_specs.extend(s.name for s in self._server_specs.get(name, []))
                await self._stop_one(name)
        # 2) 变更/新增的启用 server：重连或拉起
        for name, cfg in next_configs.items():
            if name not in prev:
                try:
                    await self._start_one(cfg)
                except Exception as e:  # noqa: BLE001
                    _log.warning("[mcp] server %r reload start failed: %s", name, e)
            elif cfg != prev[name]:
                stale_specs.extend(s.name for s in self._server_specs.get(name, []))
                await self._stop_one(name)
                try:
                    await self._start_one(cfg)
                except Exception as e:  # noqa: BLE001
                    _log.warning("[mcp] server %r reload reconnect failed: %s", name, e)
        return stale_specs

    async def _start_one(self, cfg: McpServerConfig) -> None:
        if not cfg.enabled:
            return
        client = StdioClient(cfg.command, cfg.args, cfg.env, cfg.cwd)
        await client.start()
        try:
            await client.initialize()
            tools = await client.list_tools()
        except Exception:
            await client.close()
            raise
        sem = asyncio.Semaphore(max(1, int(self.settings.concurrency)))
        specs: list[ToolSpec] = []
        for t in tools:
            adapter = McpToolAdapter(
                client=client,
                server_name=cfg.name,
                tool_name=t.name,
                description=t.description,
                inputSchema=t.inputSchema,
                timeout_sec=self.settings.tool_timeout_sec,
                semaphore=sem,
            )
            specs.append(adapter.make_spec())
        self._server_specs[cfg.name] = specs
        self._clients[cfg.name] = client
        self._semaphores[cfg.name] = sem
        _log.info("[mcp] server %r ready: %d tool(s)", cfg.name, len(tools))

    async def _stop_one(self, name: str) -> None:
        client = self._clients.pop(name, None)
        self._semaphores.pop(name, None)
        self._server_specs.pop(name, None)
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                _log.debug("[mcp] close %r failed", name)

    def register_to(self, registry: ToolRegistry) -> None:
        """把已发现的 MCP 工具注册进目标 registry（幂等：已存在的跳过）。"""
        for spec in self.specs:
            try:
                registry.register(spec)
            except ValueError:
                _log.warning("[mcp] skip duplicate/invalid spec: %s", spec.name)

    async def close(self) -> None:
        """关闭所有 Server 连接。"""
        for name in list(self._clients):
            await self._stop_one(name)
