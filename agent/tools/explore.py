"""内置探索工具：glob / find / list_dir / fetch_url（对标 Claude Code / Codex 高频只读能力）。

设计约定：
- 路径按 ``_resolve`` 逐段校验，禁止越出工作根（默认进程 cwd），与 ``fs.py`` 一致。
- 全部只读（``ToolRisk.READ``），不做任何写操作。
- 输出做上限截断（``MAX_OUTPUT_CHARS``），避免大目录/大仓库把上下文撑爆。
- 模块导入即登记到 ``default_registry``（确定性副作用）。

工具定位（供模型理解何时用）：
- ``glob``：按通配模式（如 ``**/*.py``）一次列出匹配路径，替代 shell 的 ``find``/``glob``。
- ``find``：按文件名片段/扩展名递归查找文件，适合"记不清路径、记得名字"的场景。
- ``list_dir``：列单个目录下的一级条目（文件/目录/大小），适合先看目录结构。
- ``fetch_url``：抓取一个 HTTP(S) URL 的正文，适合读文档/网页；仅文本提取。
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from agent.runtime.registry import ToolResult, ToolRisk, default_registry, tool

MAX_OUTPUT_CHARS = 8000
_MAX_GLOB_MATCHES = 200
_MAX_FIND_MATCHES = 200
_MAX_LIST_ITEMS = 200


def _resolve(root: Path, path: str) -> Path:
    """将相对路径解析为绝对路径并确保落在 root 之内（防路径遍历，与 fs.py 同）。"""
    root = root.resolve()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path escapes root: {path!r}")
    return target


def _truncate(output: str) -> str:
    if len(output) <= MAX_OUTPUT_CHARS:
        return output
    return (
        output[:MAX_OUTPUT_CHARS]
        + f"\n... [truncated: {len(output)} chars, kept first {MAX_OUTPUT_CHARS}]"
    )


@tool(
    "glob",
    risk=ToolRisk.READ,
    schema={
        "type": "object",
        "description": (
            "按通配模式列出匹配的路径（相对工作根），如 pattern='**/*.py'。"
            "支持 ** 递归、* 单段、? 单字符。默认相对工作根；结果带排序，最多返回 200 条。"
        ),
        "properties": {
            "pattern": {
                "type": "string",
                "description": "通配模式，如 '**/*.py'、'src/**/*.ts'、'*.md'",
            },
            "base": {
                "type": "string",
                "description": "可选：相对工作根的子目录作为搜索起点，默认工作根",
            },
        },
        "required": ["pattern"],
    },
)
async def glob(args: dict[str, Any]) -> ToolResult:
    pattern = args["pattern"]
    base = args.get("base") or ""
    try:
        root = Path.cwd()
        base_dir = _resolve(root, base) if base else root
        if not base_dir.is_dir():
            return ToolResult(ok=False, error=f"base is not a directory: {base!r}")
        # 用 pathlib 的 glob；** 需在 rglob 模式下才递归。
        if "**" in pattern:
            matches = sorted(p for p in base_dir.rglob(pattern))
        else:
            matches = sorted(p for p in base_dir.glob(pattern))
        matches = [p for p in matches if _root_contains(root, p)]
    except (ValueError, OSError) as e:
        return ToolResult(ok=False, error=str(e))

    if not matches:
        return ToolResult(ok=True, output=f"no matches for pattern {pattern!r}")
    if len(matches) > _MAX_GLOB_MATCHES:
        matches = matches[:_MAX_GLOB_MATCHES]
        truncated_note = f"\n... [truncated: {len(matches)} shown of {_MAX_GLOB_MATCHES}]"
    else:
        truncated_note = ""
    lines = []
    for p in matches:
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            rel = p.as_posix()
        lines.append(rel)
    out = f"{len(matches)} match(es) for pattern {pattern!r}:\n" + "\n".join(lines)
    return ToolResult(ok=True, output=_truncate(out + truncated_note))


@tool(
    "find",
    risk=ToolRisk.READ,
    schema={
        "type": "object",
        "description": (
            "按文件名片段或扩展名递归查找文件，返回相对路径。"
            "适合记不清完整路径、只记得文件名或类型时的定位。"
        ),
        "properties": {
            "name": {
                "type": "string",
                "description": "文件名片段（大小写不敏感子串匹配），如 'setup'",
            },
            "ext": {
                "type": "string",
                "description": "可选：扩展名（不含点），如 'py'、'md'；与 name 同时给时取交集",
            },
            "base": {"type": "string", "description": "可选：相对工作根的子目录作为搜索起点"},
            "max_depth": {
                "type": "integer",
                "description": "可选：递归最大深度（默认不限），避免扫太深",
            },
        },
    },
)
async def find(args: dict[str, Any]) -> ToolResult:
    name = args.get("name")
    ext = args.get("ext")
    base = args.get("base") or ""
    max_depth = args.get("max_depth")
    if not name and not ext:
        return ToolResult(ok=False, error="provide at least one of 'name' or 'ext'")
    try:
        root = Path.cwd()
        base_dir = _resolve(root, base) if base else root
        if not base_dir.is_dir():
            return ToolResult(ok=False, error=f"base is not a directory: {base!r}")
        name_lower = name.lower() if name else ""
        ext_lower = ext.lower().lstrip(".") if ext else ""
        matches: list[Path] = []
        for p in base_dir.rglob("*"):
            if not p.is_file():
                continue
            if max_depth is not None and _depth(p, base_dir) > max_depth:
                continue
            if name_lower and name_lower not in p.name.lower():
                continue
            if ext_lower and p.suffix.lower().lstrip(".") != ext_lower:
                continue
            if _root_contains(root, p):
                matches.append(p)
        matches.sort()
    except (ValueError, OSError) as e:
        return ToolResult(ok=False, error=str(e))

    if not matches:
        return ToolResult(ok=True, output=f"no files match name={name!r} ext={ext!r}")
    if len(matches) > _MAX_FIND_MATCHES:
        matches = matches[:_MAX_FIND_MATCHES]
        truncated_note = f"\n... [truncated: {len(matches)} shown of {_MAX_FIND_MATCHES}]"
    else:
        truncated_note = ""
    lines = []
    for p in matches:
        try:
            lines.append(p.relative_to(root).as_posix())
        except ValueError:
            lines.append(p.as_posix())
    out = f"{len(matches)} file(s) found:\n" + "\n".join(lines)
    return ToolResult(ok=True, output=_truncate(out + truncated_note))


@tool(
    "list_dir",
    risk=ToolRisk.READ,
    schema={
        "type": "object",
        "description": (
            "列出目录下的一级条目（文件/目录/大小），帮助先看清目录结构再深入。"
            "目录以 / 结尾区分，文件附大小；最多返回 200 条。"
        ),
        "properties": {
            "path": {
                "type": "string",
                "description": "相对工作根的目录路径；空串或 '.' 表示工作根",
            },
        },
    },
)
async def list_dir(args: dict[str, Any]) -> ToolResult:
    path = args.get("path") or "."
    try:
        root = Path.cwd()
        target = _resolve(root, path)
        if not target.is_dir():
            return ToolResult(ok=False, error=f"not a directory: {path!r}")
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except (ValueError, OSError) as e:
        return ToolResult(ok=False, error=str(e))

    items = []
    for p in entries:
        if p.is_dir():
            items.append(f"{p.name}/")
        else:
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            items.append(f"{p.name}  ({_human_size(size)})")
    if not items:
        return ToolResult(ok=True, output=f"{path} is empty")
    if len(items) > _MAX_LIST_ITEMS:
        items = items[:_MAX_LIST_ITEMS]
        truncated_note = f"\n... [truncated: {len(items)} shown of {_MAX_LIST_ITEMS}]"
    else:
        truncated_note = ""
    out = f"{path}:\n" + "\n".join(items)
    return ToolResult(ok=True, output=_truncate(out + truncated_note))


@tool(
    "fetch_url",
    risk=ToolRisk.READ,
    schema={
        "type": "object",
        "description": (
            "抓取一个 HTTP(S) URL 的正文并做简单文本提取，返回纯文本内容。"
            "适合读在线文档/README；对 HTML 做轻量去标签。超时 15s，大小上限 64KB。"
        ),
        "properties": {
            "url": {"type": "string", "description": "要抓取的完整 URL（http/https）"},
        },
        "required": ["url"],
    },
)
async def fetch_url(args: dict[str, Any]) -> ToolResult:
    url = args.get("url")
    if not url:
        return ToolResult(ok=False, error="missing required argument 'url'")
    if not isinstance(url, str):
        return ToolResult(ok=False, error="url must be a string")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ToolResult(ok=False, error=f"unsupported scheme: {parsed.scheme!r}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "work-agent/0.4"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read(64 * 1024)
            ctype = resp.headers.get("Content-Type", "")
            if "html" in ctype.lower():
                text = _html_to_text(raw.decode("utf-8", errors="replace"))
            else:
                text = raw.decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 - 网络异常种类多，统一转失败
        return ToolResult(ok=False, error=f"fetch failed: {e}")
    if not text.strip():
        return ToolResult(ok=False, error="no readable text content")
    return ToolResult(ok=True, output=_truncate(text))


def _root_contains(root: Path, p: Path) -> bool:
    """p 是否位于 root（含自身）之下。"""
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _depth(p: Path, base: Path) -> int:
    try:
        return len(p.relative_to(base).parts)
    except ValueError:
        return 0


def _human_size(n: int) -> str:
    value: float = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def _html_to_text(html: str) -> str:
    """极简 HTML → 文本：去脚本/样式、去标签、合并空白。够用即可，不强做。"""
    import re

    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# 导入即登记到默认注册表（确定性副作用）。
default_registry.register(glob)
default_registry.register(find)
default_registry.register(list_dir)
default_registry.register(fetch_url)
