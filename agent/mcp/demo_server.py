"""测试用 MCP stdio Server（验收标准）。

暴露工具（只读 + 写），通过 stdin/stdout 的 newline-delimited JSON-RPC 通信：
- ``search_files``（读）：按关键字返回匹配路径；
- ``echo``（读）：把 message 原样返回；
- ``add``（读）：两个数相加；
- ``delete_file``（写）：模拟一个写操作，演示审批走 ask。

用法（直接跑，作为真实子进程）：
    python -m agent.mcp.demo_server
或经 McpManager 配置：
    { "mcpServers": { "demo": { "command": "python", "args": ["-m", "agent.mcp.demo_server"] } } }
"""

from __future__ import annotations

import json
import sys
from typing import Any

PROTOCOL_VERSION = "2025-06-18"


def _send(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle_tools_call(params: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    name = params.get("name", "")
    args = params.get("arguments") or {}
    if name == "search_files":
        kw = args.get("keyword", "")
        return False, [{"type": "text", "text": f"found: {kw}.py" if kw else "found: none"}]
    if name == "echo":
        return False, [{"type": "text", "text": f"echo: {args.get('message', '')}"}]
    if name == "add":
        a = args.get("a", 0)
        b = args.get("b", 0)
        return False, [{"type": "text", "text": str(a + b)}]
    if name == "delete_file":
        path = args.get("path", "")
        return False, [{"type": "text", "text": f"would delete {path} (dry-run, nothing done)"}]
    return True, [{"type": "text", "text": f"unknown tool: {name}"}]


def main() -> None:
    # MCP stdio 用 UTF-8；Windows 下重配置 stdin/stdout，避免中文 JSON 乱码。
    for _stream in (sys.stdin, sys.stdout):
        _reconf = getattr(_stream, "reconfigure", None)
        if callable(_reconf):
            try:
                _reconf(encoding="utf-8")
            except (TypeError, ValueError):
                pass
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                    },
                }
            )
        elif method == "notifications/initialized":
            pass  # 握手完成，无需回包
        elif method == "tools/list":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "tools": [
                            {
                                "name": "search_files",
                                "description": "按关键字搜索文件（只读）",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"keyword": {"type": "string"}},
                                    "required": ["keyword"],
                                },
                            },
                            {
                                "name": "echo",
                                "description": "把 message 原样返回（只读）",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"message": {"type": "string"}},
                                    "required": ["message"],
                                },
                            },
                            {
                                "name": "add",
                                "description": "两个数相加（只读）",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "a": {"type": "number"},
                                        "b": {"type": "number"},
                                    },
                                    "required": ["a", "b"],
                                },
                            },
                            {
                                "name": "delete_file",
                                "description": "删除文件（写操作）",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"path": {"type": "string"}},
                                    "required": ["path"],
                                },
                            },
                        ]
                    },
                }
            )
        elif method == "tools/call":
            is_error, content = _handle_tools_call(msg.get("params") or {})
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"isError": is_error, "content": content},
                }
            )
        else:
            if rid is not None:
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32601, "message": f"method not found: {method}"},
                    }
                )


if __name__ == "__main__":
    main()
