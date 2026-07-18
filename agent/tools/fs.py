"""内置文件系统工具：read / grep / write。

约定：
- 路径按路径遍历逐段校验，禁止越出 root（默认当前进程 cwd），防止读到无关文件。
- 工具函数本身不直接 register；由本模块在导入时登记到 default_registry。
- 仅处理文本读写，二进制不在此列（后续可加）。

- ``read``：支持分页（offset/limit）+ 行号输出，避免长文件被整体截断；模型可先
  ``grep`` 定位行号，再用 ``read`` 的 offset/limit 精确读取某一段。
- ``grep``：在单文件内按正则定位匹配行（带行号与可选上下文），供模型「先匹配内容、
  再读范围」，避免一次性把大文件塞进上下文。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agent.runtime.registry import ToolResult, default_registry, tool


def _resolve(root: Path, path: str) -> Path:
    """将工具收到的相对路径解析为绝对路径，并确保落在 root 之内。

    root 默认是进程的 cwd；调用方（M1.2 测试）可通过 monkeypatch 改 cwd 来隔离。
    """
    root = root.resolve()
    target = (root / path).resolve()
    # 防路径遍历：target 必须是 root 自身或在其之下。
    if target != root and root not in target.parents:
        raise ValueError(f"path escapes root: {path!r}")
    return target


def _split_lines(text: str) -> list[str]:
    """按 \\n 切行；去掉 read_text 末尾换行产生的空尾行，使行号与文件一致。"""
    lines = text.split("\n")
    if text.endswith("\n") and lines and lines[-1] == "":
        lines.pop()
    return lines


@tool(
    "read",
    risk="read",
    schema={
        "type": "object",
        "description": (
            "读取文本文件内容。支持分页：用 offset（起始行，从 1 开始）与 limit（行数）"
            "只读取文件的一段，避免长文件被截断。输出带行号，便于配合 grep 定位后精确读取范围。"
        ),
        "properties": {
            "path": {"type": "string", "description": "相对于工作根的文件路径"},
            "offset": {
                "type": "integer",
                "description": "起始行号（1-based），默认 1；分页读取时指定从哪一行开始",
            },
            "limit": {
                "type": "integer",
                "description": "读取行数，默认读到文件末尾；与 offset 配合实现分段读取",
            },
        },
        "required": ["path"],
    },
)
async def read(args: dict[str, Any]) -> ToolResult:
    path = args["path"]
    offset = int(args.get("offset", 1))
    if offset < 1:
        offset = 1
    limit = args.get("limit")
    if limit is not None:
        limit = int(limit)
        if limit < 0:
            limit = 0
    try:
        target = _resolve(Path.cwd(), path)
        if not target.is_file():
            return ToolResult(ok=False, error=f"not a file: {path}")
        text = target.read_text(encoding="utf-8")
    except (ValueError, OSError) as e:
        return ToolResult(ok=False, error=str(e))

    lines = _split_lines(text)
    total = len(lines)
    start = offset - 1
    if start >= total:
        return ToolResult(
            ok=True,
            output=f"--- {path} (offset {offset} beyond end; total {total} lines) ---",
        )
    end = total if limit is None else min(total, start + limit)
    selected = lines[start:end]
    numbered = "\n".join(f"{start + i + 1}: {ln}" for i, ln in enumerate(selected))
    header = f"--- {path} (lines {start + 1}-{start + len(selected)} of {total}) ---"
    return ToolResult(ok=True, output=header + "\n" + numbered)


@tool(
    "grep",
    risk="read",
    schema={
        "type": "object",
        "description": (
            "在单个文件内按正则搜索匹配行，返回行号与内容（可带上下文）。"
            "用途：先 grep 定位「内容所在行号」，再用 read 的 offset/limit 精确读取该范围，"
            "避免一次性读取大文件被截断。整目录搜索请用 bash 的 grep/rg（PLAN 模式已放行）。"
        ),
        "properties": {
            "pattern": {"type": "string", "description": "正则（Python re，按行匹配）"},
            "path": {"type": "string", "description": "要搜索的文件（相对工作根）"},
            "context": {
                "type": "integer",
                "description": "匹配行上下各多显示几行（默认 0，便于看上下文）",
            },
            "ignore_case": {"type": "boolean", "description": "忽略大小写（默认 false）"},
            "max_matches": {"type": "integer", "description": "最多返回多少处匹配（默认 50），超出截断"},
        },
        "required": ["pattern", "path"],
    },
)
async def grep(args: dict[str, Any]) -> ToolResult:
    pattern = args["pattern"]
    path = args["path"]
    context = int(args.get("context", 0))
    if context < 0:
        context = 0
    ignore_case = bool(args.get("ignore_case", False))
    max_matches = int(args.get("max_matches", 50))
    if max_matches < 1:
        max_matches = 1
    try:
        target = _resolve(Path.cwd(), path)
        if not target.is_file():
            return ToolResult(ok=False, error=f"not a file: {path}")
        text = target.read_text(encoding="utf-8")
    except (ValueError, OSError) as e:
        return ToolResult(ok=False, error=str(e))

    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return ToolResult(ok=False, error=f"invalid regex: {e}")

    lines = _split_lines(text)
    total = len(lines)
    hits_all = [i for i, ln in enumerate(lines) if rx.search(ln)]
    if not hits_all:
        return ToolResult(ok=True, output=f"(no matches for {pattern!r} in {path}; {total} lines)")

    hits = hits_all[:max_matches]
    hit_set = set(hits)
    expanded: set[int] = set()
    for i in hits:
        for j in range(max(0, i - context), min(total, i + context + 1)):
            expanded.add(j)
    out_lines = [
        f"{'>' if j in hit_set else ' '} {j + 1}: {lines[j]}"
        for j in sorted(expanded)
    ]
    body = "\n".join(out_lines)
    header = (
        f"--- grep {pattern!r} in {path}: "
        f"{min(len(hits_all), max_matches)} of {len(hits_all)} matches "
        f"(file has {total} lines) ---"
    )
    return ToolResult(ok=True, output=header + "\n" + body)


@tool(
    "write",
    risk="edit",
    schema={
        "type": "object",
        "description": "把文本写入文件（自动创建父目录）。",
        "properties": {
            "path": {"type": "string", "description": "相对于工作根的文件路径"},
            "content": {"type": "string", "description": "要写入的文本"},
        },
        "required": ["path", "content"],
    },
)
async def write(args: dict[str, Any]) -> ToolResult:
    path = args["path"]
    content = args["content"]
    try:
        target = _resolve(Path.cwd(), path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except (ValueError, OSError) as e:
        return ToolResult(ok=False, error=str(e))
    return ToolResult(ok=True, output=f"wrote {len(content)} chars to {path}")


# 导入即登记到默认注册表（确定性，无副作用风险）。
default_registry.register(read)
default_registry.register(grep)
default_registry.register(write)
