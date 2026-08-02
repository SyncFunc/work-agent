"""工具注册与调度（确定性实现，AI 不在此处执行）。

设计要点：
- Tool 是原子能力：每个工具是一个 async fn(args: dict) -> ToolResult。
- 注册：@tool 装饰器 + ToolRegistry.register；工具自带 JSON Schema 自描述。
- 风险分级（read/edit/exec）仅预留字段，M2 才接审批/沙箱。
- 调度：ToolRegistry.run(name, args) 执行；unknown 抛 UnknownTool。
- 输出上限：run 支持 max_output_chars，超长截断（保护上下文）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


# 风险分级：read=只读、edit=改文件、exec=执行命令。
# 用 str 枚举（而非裸串）：类型安全、防拼错，并直接供未来 M2 审批门消费 ToolSpec.risk。
class ToolRisk(StrEnum):
    READ = "read"
    EDIT = "edit"
    EXEC = "exec"


# 由枚举导出，供 register 校验 / loop 风险门控比较（保持字符串序语义）。
RISK_LEVELS = tuple(r.value for r in ToolRisk)


@dataclass
class ToolResult:
    """工具执行结果。ok 标记成功/失败；output 为文本输出；error 为错误信息。

    diff 为可选的 unified-diff 文本（写/改类工具回传，供 UI 展示改动），不计入
    output 截断逻辑（仅用于展示，不影响模型上下文）。

    original 为写/改操作执行前的原始文件内容（仅用于前端计算实时 diff，不计入
    模型上下文）。无此语义的工具不设置。
    """

    ok: bool
    output: str = ""
    error: str | None = None
    diff: str | None = None
    original: str | None = None


@dataclass
class ToolSpec:
    """一个已注册工具的自描述规格。"""

    name: str
    fn: Callable[[dict[str, Any]], Awaitable[ToolResult]]
    risk: str = "read"
    schema: dict[str, Any] = field(default_factory=dict)
    # MCP 标记（M11.6）：True 表示由 MCP Server 适配而来。用于延迟加载（L1 目录 / L2 完整）
    # 与前端「工具来源」展示，不影响调度/审批/上下文（它们只认扁平 name）。
    is_mcp: bool = False
    # 归属的 MCP Server 名（仅 is_mcp=True 时有意义），供调用时定位连接。
    mcp_server: str | None = None

    def to_openai(self) -> dict[str, Any]:
        """导出给 OpenAI 兼容协议用的 function tool 形态（完整 schema）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.schema.get("description", ""),
                "parameters": self.schema,
            },
        }

    def to_catalog(self) -> dict[str, Any]:
        """L1 目录形态（延迟加载）：仍为合法 OpenAI function，但只带 name + 首行描述，
        参数 schema 用一个空壳（无 properties），大幅节省上下文。模型据此判断是否要
        tool_search 激活完整 schema。
        """
        desc = self.schema.get("description", "")
        first = desc.splitlines()[0][:160] if desc else self.name
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": first,
                "parameters": {"type": "object", "properties": {}},
            },
        }


class UnknownTool(KeyError):
    """请求了未注册的工具名。"""


def _cap_result(r: ToolResult, max_chars: int) -> ToolResult:
    """把超长输出/错误截断并附提示，避免撑爆上下文。ok 标记与 error 语义不变。"""
    out = r.output
    err = r.error
    changed = False
    if len(out) > max_chars:
        out = (
            out[:max_chars]
            + f"\n... [output truncated: {len(r.output)} chars, kept first {max_chars}]"
        )
        changed = True
    if err is not None and len(err) > max_chars:
        err = err[:max_chars] + " [truncated]"
        changed = True
    if not changed:
        return r
    return ToolResult(ok=r.ok, output=out, error=err, diff=r.diff, original=r.original)


class ToolRegistry:
    """工具注册表（进程内单例，确定性）。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.risk not in RISK_LEVELS:
            raise ValueError(f"invalid risk level: {spec.risk!r}; expected one of {RISK_LEVELS}")
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise UnknownTool(name)
        return spec

    def list(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def unregister(self, name: str) -> bool:
        """移除单个工具；不存在返回 False。"""
        return self._tools.pop(name, None) is not None

    def unregister_server(self, mcp_server: str) -> int:
        """移除某个 MCP Server 归属的所有工具（M11.6：MCP 重载时用）。返回移除数。"""
        removed = [n for n, s in self._tools.items() if s.is_mcp and s.mcp_server == mcp_server]
        for n in removed:
            self._tools.pop(n, None)
        return len(removed)

    async def run(
        self, name: str, args: dict[str, Any], max_output_chars: int | None = None
    ) -> ToolResult:
        """执行工具。max_output_chars 控制输出截断（保护上下文），None 表示不限制。"""
        spec = self.get(name)
        result = await spec.fn(args)
        if max_output_chars and max_output_chars > 0:
            return _cap_result(result, max_output_chars)
        return result

    def to_openai_tools(self) -> list[dict[str, Any]]:
        return [spec.to_openai() for spec in self._tools.values()]


def tool(
    name: str, risk: ToolRisk = ToolRisk.READ, schema: dict[str, Any] | None = None
) -> Callable[
    [Callable[[dict[str, Any]], Awaitable[ToolResult]]],
    ToolSpec,
]:
    """装饰器：把一个 async fn 注册为工具。

    fn 签名: async def (args: dict[str, Any]) -> ToolResult
    返回 ToolSpec（已含 name/risk/schema），调用方负责 register / 或注册到默认表。

    用法：
        @tool("bash", risk="exec", schema={...})
        async def bash(args): ...
        registry.register(bash_tool_spec)
    """

    def deco(fn: Callable[[dict[str, Any]], Awaitable[ToolResult]]) -> ToolSpec:
        return ToolSpec(
            name=name,
            fn=fn,
            risk=risk,
            schema=schema or {"type": "object", "properties": {}, "description": fn.__doc__ or ""},
        )

    return deco


# 默认全局注册表，便于工具模块在导入时自动登记。
default_registry = ToolRegistry()
