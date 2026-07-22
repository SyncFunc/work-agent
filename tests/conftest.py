"""M6.3 测试金字塔共享夹具。

markers 在 ``pyproject.toml`` 的 ``[tool.pytest.ini_options]`` 声明；此处提供 Tier1/Tier2
复用的助手（构造最小工具注册表 / 测试用 Settings），避免各测试文件重复样板。
"""

from __future__ import annotations

import asyncio

import pytest

from agent.config.settings import Settings
from agent.runtime.registry import ToolRegistry, ToolResult, tool


async def _echo(args: dict) -> ToolResult:
    return ToolResult(ok=True, output=str(args))


async def _slow(args: dict) -> ToolResult:
    await asyncio.sleep(args.get("dt", 0.01))
    return ToolResult(ok=True, output=args.get("tag", ""))


def _make_registry() -> ToolRegistry:
    """最小工具注册表（echo / slow），供 loop / tool-tapes 测试构造 AgentLoop。"""
    r = ToolRegistry()
    r.register(tool("echo", risk="read")(_echo))
    r.register(tool("slow", risk="read")(_slow))
    return r


def _settings(**kw) -> Settings:
    """测试用 Settings：收紧循环上限，便于断言终止。支持 ``loop={...}`` 局部覆盖。"""
    loop = dict(max_iterations=20, max_tool_concurrency=5, max_repeat_calls=3)
    loop.update(kw.pop("loop", {}))
    for k in (
        "max_iterations",
        "max_tool_concurrency",
        "max_repeat_calls",
        "max_tool_output_chars",
    ):
        if k in kw:
            loop[k] = kw.pop(k)
    return Settings(loop=loop, **kw)


@pytest.fixture
def make_registry():
    return _make_registry


@pytest.fixture
def settings_factory():
    return _settings


def pytest_sessionfinish(session, exitstatus):  # noqa: ANN001, ANN201, ARG001
    """测试结束后自动清理仓库根散文件，避免误提交。

    排除 ``coverage.xml``：CI 的 fast 门禁在 ``pytest --cov`` 之后还要把它当
    artifact 上传，故此处不删；其余散文件（a.txt / x / _*.txt …）一律清掉。
    """
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "cleanup_test_artifacts.py"
    if script.exists():
        subprocess.run(
            [sys.executable, str(script), "--exclude", "coverage.xml"],
            check=False,
            encoding="utf-8",
            text=True,
        )
