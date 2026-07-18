"""内置命令执行工具：bash。

约定（M1.2 阶段）：
- 通过 asyncio 创建子进程执行 shell 命令，捕获 stdout/stderr/returncode。
- 仅捕获输出，不做网络/沙箱隔离（沙箱是 M2 的独立可插拔执行层）。
- M1.2 测试用 `echo` 这类无害命令；真实环境的风险管控在 M2 接审批。
- 超时采用「与 sleep 竞速 + kill」的健壮实现（Windows ProactorEventLoop 下
  wait_for(communicate) 无法取消管道读取，故不依赖 wait_for 的取消语义）。
- Windows 上 shell 经 cmd.exe 派生子进程，仅 kill 父进程会留下持管道的孤儿，
  故超时杀进程树（taskkill /T）。
"""

from __future__ import annotations

import asyncio
import locale
import os
import re
import shutil
import sys
from typing import Any

from agent.runtime.registry import ToolResult, ToolRisk, default_registry, tool


# 按 ; && || | 切分命令为多段（每段独立判定是否只读）
_SPLIT_RE = re.compile(r"\s*(?:;|\|\||&&|\|)\s*")


def _starts_word(s: str, prefix: str) -> bool:
    """``s`` 是否以 ``prefix`` 整词开头（prefix 后须是空格或结尾）。"""
    return s == prefix or s.startswith(prefix + " ")


def is_readonly_command(cmd: str, allowlist: list[str]) -> bool:
    """判断 bash 命令是否为只读（供 PLAN 模式放行探索命令）。

    - 去掉段首的环境变量赋值（``FOO=bar cmd``）与 ``sudo``/``doas``；
    - 按 ``; && || |`` 切分为多段，每段必须落在 allowlist（支持多词前缀如 ``git status``）；
    - 任何一段含输出重定向 ``>``/``>>`` 视为写操作，直接拦截（防 ``echo x > file``）；
    - allowlist 之外的命令（``rm``/``git push``/``git commit`` 等）一律视为可变，PLAN 下拦截。
    """
    s = cmd.strip()
    # 去掉开头的环境变量赋值（``KEY=VALUE cmd``）；VALUE 为非空格串且其后跟空格才是命令
    while True:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=\S+\s+(.*)$", s)
        if not m:
            break
        s = m.group(2)
    if not s:
        return False
    for seg in _SPLIT_RE.split(s):
        seg = seg.strip()
        if not seg:
            continue
        # 归一化「看似含 '>'、实为只读」的重定向，避免误判为写操作：
        # - 黑洞重定向：>/dev/null、>>/dev/null、2>/dev/null、&>/dev/null 等仅丢弃输出；
        # - fd 合并：2>&1、>&1、1>&2 等仅把流合并到已有 fd，不写文件。
        # 归一化后若仍含 '>'，才视为重定向到**真实文件**（写操作）拦截。
        seg = re.sub(r"(?:\d*>>?|&>)\s*/dev/null", "", seg)
        seg = re.sub(r"\d*>&-?\d+", "", seg)
        seg = seg.strip()
        if not seg:
            continue
        if ">" in seg:  # 其余输出重定向 = 写真实文件
            return False
        if seg.startswith("sudo ") or seg.startswith("doas "):
            seg = seg.split(" ", 1)[1].strip()
        if not any(_starts_word(seg, a) for a in allowlist):
            return False
    return True


# --------------------------------------------------------------------------- #
# shell 解析（Windows 优先 git-bash：支持 linux 命令且输出 UTF-8；回退 cmd.exe）
# --------------------------------------------------------------------------- #
_SHELL_CACHE: tuple[list[str], bool] | None = None  # (argv 前缀, 是否 -c 模式)


def _resolve_shell() -> tuple[list[str], bool]:
    """返回 ``(prefix, use_c)``。

    - ``prefix``：传给 ``create_subprocess_exec`` 的前缀，如 ``["bash", "-c"]``；
      命令作为单个 argv 传入（不经二次 shell 解析，``&&`` / ``|`` 由该 shell 处理）。
    - ``use_c``：True 表示用 ``prefix + [cmd]``（即 ``-c`` 模式）；False 表示走 ``create_subprocess_shell(cmd)``。

    解析顺序：配置 ``bash_shell`` > 自动探测 git-bash > WSL bash > cmd.exe（Windows）/ /bin/sh（其他）。
    """
    global _SHELL_CACHE
    if _SHELL_CACHE is not None:
        return _SHELL_CACHE

    from agent.config.settings import load_settings

    explicit = load_settings().bash_shell
    if explicit:
        _SHELL_CACHE = (explicit.split() + ["-c"], True)
        return _SHELL_CACHE

    if sys.platform == "win32":
        # git-bash 的 bash.exe 通常在 Git\bin 下；也接受 PATH 中名字含 git 的 bash。
        candidates = [
            r"D:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ]
        found = next((c for c in candidates if os.path.isfile(c)), None)
        if found is None:
            gb = shutil.which("bash")
            if gb and "git" in gb.lower():
                found = gb
        if found:
            _SHELL_CACHE = ([found, "-c"], True)
            return _SHELL_CACHE
        # 兜底：cmd.exe（注意不保证支持 ls 等 linux 命令）
        _SHELL_CACHE = (["cmd.exe", "/c"], True)
        return _SHELL_CACHE

    _SHELL_CACHE = (["/bin/sh", "-c"], True)
    return _SHELL_CACHE


def _decode(b: bytes) -> str:
    """优先 UTF-8（git-bash 默认）；失败回退系统本地编码（如 GBK），再不行替换。"""
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode(locale.getpreferredencoding(False), errors="replace")


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """杀掉进程（含 Windows 上的子进程树），使管道尽快关闭。"""
    if sys.platform == "win32":
        # taskkill /T 杀整棵树，/F 强制；避免 cmd.exe 派生的子进程变孤儿持管道。
        try:
            await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            pass
    else:
        proc.kill()


@tool(
    "bash",
    risk=ToolRisk.EXEC,
    schema={
        "type": "object",
        "description": "在 shell 中执行命令，返回 stdout/stderr/返回码。",
        "properties": {
            "cmd": {"type": "string", "description": "要执行的 shell 命令"},
            "timeout": {
                "type": "integer",
                "description": "超时秒数（可选，默认 30）",
            },
        },
        "required": ["cmd"],
    },
)
async def bash(args: dict[str, Any]) -> ToolResult:
    cmd = args["cmd"]
    timeout = args.get("timeout", 30)

    # git-bash 等需在 UTF-8 环境输出，避免中文乱码；继承当前环境变量并强制 UTF-8 locale。
    env = dict(os.environ)
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"

    prefix, use_c = _resolve_shell()
    try:
        if use_c:
            # prefix = ["bash", "-c"]；cmd 作为单个 argv 传给 shell，不经二次解析。
            proc = await asyncio.create_subprocess_exec(
                *prefix, cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
    except OSError as e:
        return ToolResult(ok=False, error=str(e))

    comm_task = asyncio.ensure_future(proc.communicate())
    timer = asyncio.ensure_future(asyncio.sleep(timeout))
    try:
        done, _ = await asyncio.wait(
            {comm_task, timer}, return_when=asyncio.FIRST_COMPLETED
        )
    except (asyncio.CancelledError, Exception):
        timer.cancel()
        raise

    if comm_task in done:
        timer.cancel()
        try:
            stdout_b, stderr_b = comm_task.result()
        except OSError as e:
            return ToolResult(ok=False, error=str(e))
    else:
        # 超时：杀进程树；被杀后管道关闭，communicate 会迅速收尾。
        await _kill_tree(proc)
        try:
            stdout_b, stderr_b = await asyncio.wait_for(comm_task, timeout=2)
        except (asyncio.TimeoutError, OSError):
            stdout_b = b""
            stderr_b = b""
        return ToolResult(ok=False, error=f"command timed out after {timeout}s")

    out = _decode(stdout_b) if stdout_b else ""
    stderr = _decode(stderr_b) if stderr_b else ""
    rc = proc.returncode or 0
    if stderr:
        out = out + ("" if out.endswith("\n") else "\n") + f"[stderr]\n{stderr}"
    return ToolResult(ok=rc == 0, output=out, error=None if rc == 0 else f"exit code {rc}")


default_registry.register(bash)
