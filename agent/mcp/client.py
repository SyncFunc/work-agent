"""MCP stdio 客户端：走子进程 stdin/stdout 的 newline-delimited JSON-RPC 2.0。

设计约定（对齐 MCP 官方 stdio transport）：
- 消息以换行分隔的 JSON 交换，方法名遵循 ``资源/动作`` 约定；
- 握手：``initialize``(带协议版本+capabilities) → server 回 version+capabilities →
  client 发 ``notifications/initialized`` 通知，之后才进入 Operation 阶段；
- 传输层只用标准库（asyncio + json + subprocess），不引入外部依赖。

用法（由 MCPManager 持有，生命周期单例）：
    client = StdioClient(command, args, env)
    await client.start()
    await client.initialize()      # 握手
    tools = await client.list_tools()
    result = await client.call_tool("x", {"a": 1})
    await client.close()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)

# 我们声明支持的协议版本（MCP 按日期版本化）。server 会协商到它支持的版本。
PROTOCOL_VERSION = "2025-06-18"


class McpError(Exception):
    """MCP 层错误（JSON-RPC error 响应 / 协议异常）。"""


@dataclass
class McpTool:
    """MCP tools/list 返回的一个工具定义。"""

    name: str
    description: str = ""
    inputSchema: dict[str, Any] = field(default_factory=dict)


@dataclass
class McpCallResult:
    """tools/call 的返回。isError 表示 server 侧执行出错；content 为结果元素数组。"""

    isError: bool
    content: list[dict[str, Any]]

    def text(self) -> str:
        """把 content 里的文本元素拼接成纯文本（二进制/图片暂不支持 → 占位）。"""
        parts: list[str] = []
        for item in self.content:
            ctype = item.get("type")
            if ctype == "text":
                parts.append(item.get("text", ""))
            elif ctype in ("image", "resource", "audio"):
                parts.append(f"[{ctype} content not supported]")
            else:
                parts.append(str(item.get("text", item)))
        return "\n".join(p for p in parts if p)


class StdioClient:
    """管理一个 MCP stdio Server 子进程，提供 JSON-RPC 读写。"""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._id = 0
        self._closed = False

    # -- 生命周期 -------------------------------------------------------- #
    async def start(self) -> None:
        """拉起子进程并启动读循环。"""
        full_env = os.environ.copy()
        for k, v in self.env.items():
            if v is not None:
                full_env[k] = v
        self._proc = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=full_env,
            cwd=self.cwd,
        )
        self._reader = self._proc.stdout
        self._writer = self._proc.stdin
        self._read_task = asyncio.ensure_future(self._read_loop())

    async def initialize(self) -> dict[str, Any]:
        """握手：initialize → 协商协议版本与能力。"""
        resp = await self.request(
            "initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}}
        )
        # 通知 server 握手完成。
        await self.notify("notifications/initialized", {})
        return resp

    async def list_tools(self) -> list[McpTool]:
        resp = await self.request("tools/list", {})
        tools = []
        for item in resp.get("tools", []):
            tools.append(
                McpTool(
                    name=str(item.get("name", "")),
                    description=str(item.get("description", "")),
                    inputSchema=item.get("inputSchema") or {},
                )
            )
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallResult:
        resp = await self.request("tools/call", {"name": name, "arguments": arguments or {}})
        return McpCallResult(
            isError=bool(resp.get("isError", False)),
            content=resp.get("content") or [],
        )

    async def close(self) -> None:
        """关闭连接：关闭 stdin、取消读循环、终止子进程。

        显式 close stdin writer 并等待读任务结束，确保 Windows Proactor 的
        pipe transport 被释放，避免 PytestUnraisableExceptionWarning(unclosed transport)。
        """
        self._closed = True
        # 关闭 stdin writer（通知 server 不再有输入，并释放 pipe）
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(McpError("mcp client closed"))
        self._pending.clear()
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except (TimeoutError, ProcessLookupError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass

    # -- JSON-RPC 原语 ---------------------------------------------------- #
    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """发一个带 id 的 request，等待对应 response（含 error 则抛 McpError）。"""
        if self._closed or self._proc is None or self._writer is None:
            raise McpError("mcp client not started or closed")
        self._id += 1
        rid = self._id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        await self._write(msg)
        try:
            resp = await fut
        finally:
            self._pending.pop(rid, None)
        if "error" in resp and resp.get("error"):
            err = resp["error"]
            raise McpError(f"mcp request {method} failed: {err.get('message', err)}")
        return resp.get("result") or {}

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """发一个不带 id 的 notification（不等响应）。"""
        if self._closed or self._writer is None:
            raise McpError("mcp client not started or closed")
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, msg: dict[str, Any]) -> None:
        assert self._writer is not None
        self._writer.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        await self._writer.drain()

    async def _read_loop(self) -> None:
        """持续读 stdout，按 id 分发 response / 处理 notifications。"""
        assert self._reader is not None
        try:
            while not self._closed:
                line = await self._reader.readline()
                if not line:
                    break
                raw = line.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    _log.warning("[mcp] bad json line: %r", raw[:200])
                    continue
                await self._dispatch(msg)
        except (asyncio.CancelledError, ConnectionResetError, OSError):
            pass
        finally:
            # 读循环结束 → 所有 pending 标记失败。
            err = McpError("mcp server closed")
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(err)
            self._pending.clear()

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg:
            fut = self._pending.get(msg["id"])
            if fut is not None and not fut.done():
                fut.set_result(msg)
        # notification（无 id）目前忽略；若有 server→client request 后续按需扩展。
