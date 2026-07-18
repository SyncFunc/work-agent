"""M1.2 验收：内置 read/grep/write/bash 在临时目录中的行为（不碰真实文件系统根）。"""

from pathlib import Path

import pytest

from agent.tools import fs  # 导入触发 read/grep/write 注册
from agent.runtime.registry import default_registry


@pytest.fixture
def tmp_root(tmp_path: Path, monkeypatch):
    """把进程 cwd 指向临时目录，使 read/write 的工作根隔离在 tmp_path。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


async def test_write_then_read_roundtrip(tmp_root: Path):
    reg = default_registry
    w = await reg.run("write", {"path": "a.txt", "content": "hello world"})
    assert w.ok, w.error
    assert (tmp_root / "a.txt").read_text(encoding="utf-8") == "hello world"

    # read 现在带行号输出（便于配合 grep 定位后精确读取），故断言含行号形式。
    r = await reg.run("read", {"path": "a.txt"})
    assert r.ok
    assert "hello world" in r.output
    assert "1: hello world" in r.output


async def test_read_paginate_offset_limit(tmp_root: Path):
    """read 支持 offset/limit 分段读取，避免长文件被截断。"""
    reg = default_registry
    content = "\n".join(f"line{i}" for i in range(1, 11)) + "\n"
    await reg.run("write", {"path": "b.txt", "content": content})

    r = await reg.run("read", {"path": "b.txt", "offset": 3, "limit": 3})
    assert r.ok
    assert "lines 3-5 of 10" in r.output
    assert "3: line3" in r.output
    assert "5: line5" in r.output
    assert "6: line6" not in r.output  # 超出 limit 的部分不返回


async def test_read_offset_beyond_end(tmp_root: Path):
    reg = default_registry
    await reg.run("write", {"path": "c.txt", "content": "only\n"})
    r = await reg.run("read", {"path": "c.txt", "offset": 99})
    assert r.ok and "beyond end" in r.output


async def test_grep_finds_lines_with_numbers(tmp_root: Path):
    """grep 返回带行号的匹配行，供模型定位后再 read 精确范围。"""
    reg = default_registry
    content = "\n".join([
        "import os",
        "def foo():",
        "    return os.getcwd()",
        "class Bar:",
        "    def get(self): return os.getcwd()",
    ]) + "\n"
    await reg.run("write", {"path": "g.py", "content": content})

    r = await reg.run("grep", {"pattern": "os\\.getcwd", "path": "g.py"})
    assert r.ok
    assert "3:     return os.getcwd()" in r.output
    assert "5:     def get(self): return os.getcwd()" in r.output
    assert "2 of 2 matches" in r.output
    assert "file has 5 lines" in r.output


async def test_grep_context_and_no_match(tmp_root: Path):
    reg = default_registry
    await reg.run("write", {"path": "n.txt", "content": "a\nb\nc\nMATCH\nd\ne\n"})
    r = await reg.run("grep", {"pattern": "MATCH", "path": "n.txt", "context": 1})
    assert r.ok
    assert "3: c" in r.output       # 上一条上下文
    assert "4: MATCH" in r.output   # 匹配行（> 标记）
    assert "5: d" in r.output       # 下一条上下文

    r2 = await reg.run("grep", {"pattern": "zzz", "path": "n.txt"})
    assert r2.ok and "no matches" in r2.output


async def test_write_creates_parent_dirs(tmp_root: Path):
    reg = default_registry
    w = await reg.run("write", {"path": "sub/dir/b.txt", "content": "x"})
    assert w.ok, w.error
    assert (tmp_root / "sub" / "dir" / "b.txt").exists()


async def test_read_missing_file_fails(tmp_root: Path):
    reg = default_registry
    r = await reg.run("read", {"path": "nope.txt"})
    assert not r.ok and r.error


async def test_read_path_traversal_rejected(tmp_root: Path):
    reg = default_registry
    # 尝试逃逸到临时根之外的路径
    r = await reg.run("read", {"path": "../../etc/passwd"})
    assert not r.ok and "escapes root" in r.error


async def test_bash_echo_captures_stdout(tmp_root: Path):
    reg = default_registry
    r = await reg.run("bash", {"cmd": "echo hi"})
    assert r.ok, r.error
    assert "hi" in r.output


async def test_bash_nonzero_exit_reports_error(tmp_root: Path):
    reg = default_registry
    r = await reg.run("bash", {"cmd": "exit 7"})
    assert not r.ok
    assert "exit code 7" in (r.error or "")


async def test_bash_captures_stderr(tmp_root: Path):
    reg = default_registry
    r = await reg.run("bash", {"cmd": "echo oops 1>&2"})
    assert r.ok
    assert "oops" in r.output and "[stderr]" in r.output


async def test_bash_timeout(tmp_root: Path):
    # 用跨平台且必然阻塞 5s 的命令验证超时（Windows 上 sleep 不是有效命令）。
    # 注意：bash 工具捕获超时并以 ToolResult(ok=False) 形式返回，不向上抛异常。
    r = await default_registry.run("bash", {"cmd": "python -c \"import time; time.sleep(5)\"", "timeout": 1})
    assert not r.ok and "timed out" in r.error
