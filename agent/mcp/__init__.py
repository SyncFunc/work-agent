"""MCP（Model Context Protocol）接入模块（M11.6）。

把外部 MCP Server 暴露的工具翻译成 work-agent 的 ToolSpec，接入现有调度/审批/上下文。
MVP 只支持 stdio 本地 Server；Streamable HTTP / Resources / Prompts 放二期。

组件：
- ``config.McpServerConfig`` / ``load_servers``：读 .agent/mcp.json → Server 定义。
- ``client.StdioClient``：stdio 子进程 JSON-RPC 客户端（握手 / tools/list / tools/call）。
- ``adapter.McpToolAdapter``：MCP 工具 → ToolSpec（mcp__server__tool 命名 + risk 推断 + content 抽取）。
- ``manager.McpManager``：生命周期 + 发现 + 注册进 ToolRegistry。
"""

from agent.mcp.adapter import McpToolAdapter, _infer_risk, safe_name
from agent.mcp.client import McpCallResult, McpError, McpTool, StdioClient
from agent.mcp.config import McpServerConfig, load_servers
from agent.mcp.manager import McpManager

__all__ = [
    "McpManager",
    "McpToolAdapter",
    "McpServerConfig",
    "McpTool",
    "McpCallResult",
    "McpError",
    "StdioClient",
    "load_servers",
    "_infer_risk",
    "safe_name",
]
