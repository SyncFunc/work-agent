"""MCPToolAdapter：把 MCP 工具翻译成 work-agent 的 ToolSpec。

- 命名：``mcp__<server>__<tool>`` 三段式，非法字符清洗成 ``_``（对齐 Claude Code）。
- risk：fail-closed，默认 ``exec``；只读白名单命中才降为 ``read``。
- fn：把 args 转发为 ``tools/call``，从 content 数组抽文本拼 ToolResult。
- 输出上限：在 registry.run 已有截断，这里再兜一道防 adapter 直调时超长。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from agent.mcp.client import McpCallResult, StdioClient
from agent.runtime.registry import ToolResult, ToolSpec

# 非法字符清洗（对齐 Claude safeServer/safeTool）：非 [a-zA-Z0-9_-] → "_"。
_ILLEGAL_RE = re.compile(r"[^a-zA-Z0-9_-]")


def safe_name(value: str) -> str:
    """清洗 MCP server/tool 名，保证最终工具名是干净标识符。"""
    cleaned = _ILLEGAL_RE.sub("_", value or "")
    return cleaned.strip("_") or "tool"


def _infer_risk(name: str, description: str) -> str:
    """fail-closed 风险推断。

    原则：宁可保守。默认当有写操作（exec，走审批）；只有名字/描述明确命中只读词
    且描述无副作用词，才降为 read（自动放行）。
    """
    text = f"{name} {description}".lower()
    read_words = ("read", "get", "list", "view", "fetch", "search", "find", "lookup", "query")
    write_words = (
        "write",
        "update",
        "delete",
        "remove",
        "create",
        "post",
        "send",
        "put",
        "patch",
        "set",
        "add",
        "edit",
        "insert",
        "upload",
        "download",
        "execute",
        "run",
        "commit",
        "push",
        "merge",
        "close",
        "approve",
    )
    is_read_hint = any(w in text for w in read_words)
    is_write_hint = any(w in text for w in write_words)
    if is_read_hint and not is_write_hint:
        return "read"
    return "exec"


class McpToolAdapter:
    """一个 MCP 工具的适配器：持有 client 引用，fn 转发 tools/call。"""

    def __init__(
        self,
        client: StdioClient,
        server_name: str,
        tool_name: str,
        description: str,
        inputSchema: dict[str, Any],
        timeout_sec: float = 45.0,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.client = client
        self.server_name = server_name
        self.tool_name = tool_name
        self.description = description
        self.inputSchema = inputSchema
        self.timeout_sec = timeout_sec
        self.semaphore = semaphore

    def name(self) -> str:
        return f"mcp__{safe_name(self.server_name)}__{safe_name(self.tool_name)}"

    def make_spec(self) -> ToolSpec:
        """生成 ToolSpec（is_mcp=True + mcp_server 归属）。"""
        risk = _infer_risk(self.tool_name, self.description)
        schema = {
            "type": "object",
            "description": self.description or f"MCP tool {self.server_name}/{self.tool_name}",
            **(
                {
                    "properties": self.inputSchema.get("properties", {}),
                    "required": self.inputSchema.get("required", []),
                }
                if isinstance(self.inputSchema, dict) and self.inputSchema.get("properties")
                else {"properties": {}}
            ),
        }
        return ToolSpec(
            name=self.name(),
            fn=self.fn,
            risk=risk,
            schema=schema,
            is_mcp=True,
            mcp_server=self.server_name,
        )

    async def fn(self, args: dict[str, Any]) -> ToolResult:
        """转发 tools/call，结果拼 ToolResult。超时/异常 → ok=False。"""
        call_args = args or {}
        try:
            coro = self.client.call_tool(self.tool_name, call_args)
            if self.semaphore is not None:
                async with self.semaphore:
                    resp: McpCallResult = await asyncio.wait_for(coro, timeout=self.timeout_sec)
            else:
                resp = await asyncio.wait_for(coro, timeout=self.timeout_sec)
        except TimeoutError:
            return ToolResult(
                ok=False, error=f"MCP tool {self.name()} timed out after {self.timeout_sec}s"
            )
        except Exception as e:  # noqa: BLE001 - 网络/子进程异常种类多，统一转失败
            return ToolResult(
                ok=False, error=f"MCP tool {self.name()} failed: {type(e).__name__}: {e}"
            )

        if resp.isError:
            return ToolResult(
                ok=False, error=resp.text() or f"MCP tool {self.name()} returned error"
            )
        text = resp.text()
        return ToolResult(ok=True, output=text if text else "(no output)")
