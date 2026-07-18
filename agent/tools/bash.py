"""内置命令执行工具：bash（M2.1+ 经沙箱执行层）。

- 统一经可插拔沙箱执行层 ``get_executor()`` 运行（默认 ``LocalExecutor``，测试可注入
  ``FakeExecutor``）；不直接 ``subprocess``。
- 超时 / 杀进程树 / UTF-8 解码等由执行层（``sandbox.py``）负责；本模块只构造
  ``ExecRequest`` 并转回 ``ToolResult``。
- 审批通过后需临时提升沙箱时，由 ``loop`` 直接以 ``elevated_profile`` 构造请求执行。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from agent.runtime.registry import ToolResult, ToolRisk, default_registry, tool
from agent.runtime.sandbox import ExecRequest, get_executor


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
    """执行 shell 命令（M2.1+）：统一经可插拔沙箱执行层 ``get_executor()``。

    ``get_executor()`` 模块级缓存（默认 ``LocalExecutor``，可被测试注入 ``FakeExecutor``），
    不在此处直接 ``subprocess``；profile 由执行器持有，本工具只取 ``default_profile`` 填入
    ``ExecRequest``。审批通过后需临时提升沙箱时，由 ``loop`` 直接构造带 ``elevated_profile``
    的 ``ExecRequest`` 执行（见 ``loop._run_bash_in_sandbox``），不经本函数。
    """
    cmd = args["cmd"]
    timeout = args.get("timeout", 30)

    # git-bash 等需在 UTF-8 环境输出，避免中文乱码；继承当前环境变量并强制 UTF-8 locale。
    env = dict(os.environ)
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"

    executor = get_executor()
    profile = executor.default_profile
    req = ExecRequest(cmd=cmd, cwd=Path.cwd(), env=env, timeout=timeout, profile=profile)
    r = await executor.run(req)
    # 直接透传执行层的输出与错误：sandbox 的 ExecResult.error 已含语义信息
    # （如 "command timed out after 1s" 或 "沙箱拦截：..."），成功时为 None。
    return ToolResult(ok=r.ok, output=r.output, error=r.error)


default_registry.register(bash)
