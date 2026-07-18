"""OS 级可插拔执行层（M2.1）：把"命令在哪跑、能碰什么、能否联网"从 ``bash`` 工具抽离。

设计依据：[`../../knowledge/sandbox-approval-design.md`](../../knowledge/sandbox-approval-design.md)
（Codex 模式：``local``/``docker``/``external`` 执行器 + 三档 profile + 应用层 ``CommandFilter``）。

核心思想（P2 安全在边界不在提示）：
- 执行器是**可插拔抽象**（`Executor` Protocol）。``bash`` 工具（M2.4 接入）只构造 ``ExecRequest``
  并调 ``get_executor().run()``，不直接 ``subprocess``；测试用 ``FakeExecutor`` 替换，确定性、不依赖 root/网络。
- 三档 profile（``read-only`` / ``workspace-write`` / ``danger-full``）：网络**默认拒绝**，仅 ``danger-full`` 放开。
- ``LocalExecutor`` 的隔离手段按**运行时内核**选择，而非"是否 Windows 机器"：
  - Linux（含 WSL2，``os.uname().sysname == "Linux"``）：最佳努力用 ``unshare -n`` 建**无网命名空间**（零依赖断网），
    再加应用层 ``CommandFilter`` 做越界写/破坏性拦截的**纵深防御**；``unshare`` 不可用时**降级**为进程级 + ⚠️告警，**绝不抛异常中断 Agent**。
  - 原生 Windows / macOS（无 Landlock/seccomp 内核原语）：走 ``CommandFilter`` 应用层主动拦截
    （越界写/联网/破坏性 → ``ok=False``，**不打印告警**）。真隔离靠 ``docker``/``external``。

> 诚实边界：M2.1 的 Linux 强隔离 = ``unshare -n``（断网，真实）+ ``CommandFilter``（写/破坏性，应用层）；
> 内核级 Landlock/seccomp 的 Python 绑定（``landlock``/``seccomp``）留作后续可选增强（import 失败即跳过，不影响本模块）。
> 原生 Windows/macOS 当前以 ``CommandFilter`` 应用层强制为主，文档/注释诚实标注，不假装 OS 级。
"""

from __future__ import annotations

import asyncio
import locale
import logging
import os
import re
import shutil
import shlex
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

_log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 三档 profile
# --------------------------------------------------------------------------- #
class SandboxProfile(str, Enum):
    """沙箱档位（执行时强制的隔离强度 / 权限边界）。"""

    READ_ONLY = "read-only"          # 任意读，禁写，断网
    WORKSPACE_WRITE = "workspace-write"  # 读任意；仅 cwd 可写；断网
    DANGER_FULL = "danger-full"      # 完全访问，放行网络（用户显式接受风险）


# --------------------------------------------------------------------------- #
# 请求 / 结果（与 ToolResult 形态对齐：ok / output / error）
# --------------------------------------------------------------------------- #
@dataclass
class ExecRequest:
    cmd: str
    cwd: Path
    env: dict[str, str]
    timeout: int = 30
    profile: SandboxProfile = SandboxProfile.WORKSPACE_WRITE


@dataclass
class ExecResult:
    ok: bool
    output: str
    error: str | None
    returncode: int
    sandbox: str            # 实际执行器名（trace 用）


# --------------------------------------------------------------------------- #
# 执行器协议
# --------------------------------------------------------------------------- #
@runtime_checkable
class Executor(Protocol):
    """Agent 命令执行抽象：一个具名执行器 + 异步 run。"""

    name: str

    async def run(self, req: ExecRequest) -> ExecResult:
        ...


# --------------------------------------------------------------------------- #
# 应用层命令过滤（CommandFilter）：spawn 前静态分析的软沙箱
# --------------------------------------------------------------------------- #
@dataclass
class FilterVerdict:
    blocked: bool
    reason: str | None = None   # 被哪条规则拦（用于 ExecResult.error）


# 命令切分（与 bash.is_readonly_command 同思路：按 ; && || | 切多段）
_SPLIT_RE = re.compile(r"\s*(?:;|\|\||&&|\|)\s*")

# 联网命令（命中即视为需要网络）
_NETWORK_BINS = {
    "curl", "wget", "wget2", "ssh", "scp", "sftp", "rsync", "telnet", "nc",
    "ncat", "netcat", "ftp", "ping", "ping6", "traceroute", "dig", "nslookup",
}
# 包管理器联网子命令（install/update/... 才联网；如 list 不联网）
_NETWORK_PKG = {
    "npm", "yarn", "pnpm", "pip", "pip3", "pipenv", "poetry", "gem", "apk",
    "apt", "apt-get", "dnf", "yum", "brew", "conda", "composer",
}
_NETWORK_PKG_SUBCMD = {"install", "update", "upgrade", "add", "search", "fetch", "push"}

# 写类命令
_WRITE_BINS = {"cp", "mv", "install", "mkdir", "touch", "ln", "rm", "rmdir", "dd", "tee", "chmod", "chown"}

# 破坏性命令（命中即禁，与是否在 cwd 内无关）
_CATASTROPHIC = [
    r":\(\)\s*\{",                 # fork bomb
    r"\bdd\b[^|]*\bof=/dev/",      # dd 写设备
    r"\bmkfs",                     # 建文件系统
    r"\bshutdown\b", r"\breboot\b", r"\bhalt\b", r"\bpoweroff\b",
    r">\s*/dev/sd",                # 覆写磁盘
    r"\bchmod\s+-R\s+777\s+/",     # chmod -R 777 /
]


def _is_catastrophic(cmd: str) -> bool:
    return any(re.search(p, cmd) for p in _CATASTROPHIC)


def _analyze(cmd: str) -> tuple[bool, list[str]]:
    """整条命令的静态分析：返回 ``(has_network, write_targets)``。

    write_targets 为检测到的写目标路径串（重定向 ``>`` / ``>>`` 目标，或 cp/mv/rm/... 的目标参数）。
    """
    has_network = False
    write_targets: list[str] = []
    for st in _SPLIT_RE.split(cmd):
        st = st.strip()
        if not st:
            continue
        # 去掉段首环境变量赋值（``FOO=bar cmd``）
        while True:
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=\S+\s+(.*)$", st)
            if not m:
                break
            st = m.group(2).strip()
        if not st:
            continue
        # 归一化：黑洞重定向（>/dev/null，含 &> 形式）与 fd 合并（2>&1）仅丢弃输出，不算写真实文件。
        norm = re.sub(r"(?:\d*>>?|&>)\s*/dev/null", "", st)
        norm = re.sub(r"\d*>&-?\d+", "", norm).strip()
        if not norm:
            continue
        # 把剩余的 &> 当作普通 > 重定向（以便捕获写目标）
        norm = re.sub(r"&>", "> ", norm)
        # 重定向真实文件（写目标）；仅匹配非 & 前导的 >（fd 合并已在上一步剔除）
        for m in re.finditer(r"(?<!&)(>>?)\s*(\S+)", norm):
            target = m.group(2)
            if target.startswith("&") or target == "/dev/null":
                continue
            write_targets.append(target)
        # 命令分析：去掉重定向噪音后按 token 判定
        cmd_part = re.sub(r">>?\s*\S+", " ", norm)
        try:
            toks = shlex.split(cmd_part, posix=True)
        except ValueError:
            toks = cmd_part.split()
        if not toks:
            continue
        cmd0 = toks[0]
        if cmd0 in _NETWORK_BINS:
            has_network = True
        elif cmd0 in _NETWORK_PKG and any(t in _NETWORK_PKG_SUBCMD for t in toks[1:]):
            has_network = True
        elif cmd0 == "git" and any(
            t in ("clone", "fetch", "pull", "push", "ls-remote", "submodule") for t in toks[1:]
        ):
            has_network = True
        if cmd0 in _WRITE_BINS:
            if cmd0 in ("rm", "rmdir", "mkdir", "touch", "tee", "chmod", "chown"):
                for t in toks[1:]:
                    if not t.startswith("-"):
                        write_targets.append(t)
            elif cmd0 == "dd":
                for t in toks[1:]:
                    if t.startswith("of="):
                        write_targets.append(t[3:])
            else:  # cp / mv / install / ln：最后一个非选项参数为目标
                non_opt = [t for t in toks[1:] if not t.startswith("-")]
                if non_opt:
                    write_targets.append(non_opt[-1])
    return has_network, write_targets


def _is_within(path_str: str, cwd: Path) -> bool:
    """路径是否落在 ``cwd`` 内（含 cwd 本身）。解析失败按"在内部"处理，避免误拦相对路径。"""
    try:
        p = Path(path_str).expanduser()
        p = p.resolve() if p.is_absolute() else (cwd / p).resolve()
        base = cwd.resolve()
        return p == base or base in p.parents
    except (OSError, RuntimeError):
        return True


class CommandFilter:
    """应用层命令沙箱：spawn 前静态分析命令，按 profile 主动拦截。

    原生 Windows / macOS 的主隔离手段；Linux 下作为 OS 沙箱（``unshare -n``）的纵深防御。
    拦截时**不打印告警**——直接返回 ``blocked`` 由执行器转成 ``ExecResult(ok=False)``。
    """

    def __init__(self, *, workspace: Path) -> None:
        self._workspace = Path(workspace)

    def check(self, cmd: str, profile: SandboxProfile, *, cwd: Path) -> FilterVerdict:
        if profile is SandboxProfile.DANGER_FULL:
            return FilterVerdict(blocked=False)
        has_network, write_targets = _analyze(cmd)
        if has_network:
            return FilterVerdict(True, "沙箱拦截：断网 profile 禁止网络访问")
        if write_targets:
            if profile is SandboxProfile.READ_ONLY:
                return FilterVerdict(True, "沙箱拦截：read-only profile 禁止任何写入")
            for t in write_targets:
                if not _is_within(t, cwd):
                    return FilterVerdict(True, f"沙箱拦截：越界写 {t}")
        if _is_catastrophic(cmd):
            return FilterVerdict(True, "沙箱拦截：破坏性命令被禁止")
        return FilterVerdict(blocked=False)


# --------------------------------------------------------------------------- #
# shell 解析（与 bash._resolve_shell 同策略，但 self-contained 不反向依赖 bash 工具，
# 避免 M2.4 接入时形成 sandbox ↔ bash 的循环导入）
# --------------------------------------------------------------------------- #
def _resolve_shell() -> tuple[list[str], bool]:
    """返回 ``(prefix, use_c)``：``prefix`` 为 ``["bash","-c"]`` 等，-c 模式传单条命令。"""
    if sys.platform == "win32":
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
            return ([found, "-c"], True)
        return (["cmd.exe", "/c"], True)
    return (["/bin/sh", "-c"], True)


def _decode(b: bytes) -> str:
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode(locale.getpreferredencoding(False), errors="replace")


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if sys.platform == "win32":
        try:
            await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            pass
    else:
        proc.kill()


async def _run_subprocess(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int, label: str
) -> ExecResult:
    """通用子进程执行（超时竞速 + 杀进程树 + UTF-8 解码），返回 ExecResult。"""
    full_env = dict(os.environ)
    full_env["LANG"] = "C.UTF-8"
    full_env["LC_ALL"] = "C.UTF-8"
    full_env.update(env or {})
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
        )
    except OSError as e:
        return ExecResult(ok=False, output="", error=str(e), returncode=-1, sandbox=label)

    comm_task = asyncio.ensure_future(proc.communicate())
    timer = asyncio.ensure_future(asyncio.sleep(timeout))
    try:
        done, _ = await asyncio.wait({comm_task, timer}, return_when=asyncio.FIRST_COMPLETED)
    except (asyncio.CancelledError, Exception):
        timer.cancel()
        raise

    if comm_task in done:
        timer.cancel()
        try:
            stdout_b, stderr_b = comm_task.result()
        except OSError as e:
            return ExecResult(ok=False, output="", error=str(e), returncode=-1, sandbox=label)
    else:
        await _kill_tree(proc)
        try:
            stdout_b, stderr_b = await asyncio.wait_for(comm_task, timeout=2)
        except (asyncio.TimeoutError, OSError):
            stdout_b = b""
            stderr_b = b""
        return ExecResult(ok=False, output="", error=f"command timed out after {timeout}s",
                          returncode=-1, sandbox=label)

    out = _decode(stdout_b) if stdout_b else ""
    err = _decode(stderr_b) if stderr_b else ""
    rc = proc.returncode or 0
    if err:
        out = out + ("" if out.endswith("\n") else "\n") + f"[stderr]\n{err}"
    return ExecResult(
        ok=rc == 0,
        output=out,
        error=None if rc == 0 else f"exit code {rc}",
        returncode=rc,
        sandbox=label,
    )


# --------------------------------------------------------------------------- #
# 执行器实现
# --------------------------------------------------------------------------- #
class LocalExecutor:
    """本地执行器：按运行时内核选择隔离手段（Linux=unshare -n + CommandFilter；其余=CommandFilter）。"""

    name = "local"

    def __init__(
        self,
        *,
        workspace: Path,
        profile: SandboxProfile = SandboxProfile.WORKSPACE_WRITE,
    ) -> None:
        self._workspace = Path(workspace)
        self._profile = profile
        self._filter = CommandFilter(workspace=self._workspace)
        self._shell = _resolve_shell()
        self._isolation = self._choose_isolation()

    def _choose_isolation(self) -> str:
        """返回 "linux-kernel" 或 "app-layer"。依据运行时内核，而非"是否 Windows 机器"。"""
        if hasattr(os, "uname"):
            sysname = os.uname().sysname
            if sysname == "Linux":
                # WSL2 也报 Linux 且内核 ≥5.13，自动命中强隔离分支
                if self._unshare_available():
                    return "linux-kernel"
                _log.warning("unshare 网络命名空间不可用，LocalExecutor 降级为进程级执行（无 OS 网络隔离）")
                return "app-layer"
        # 原生 Windows / macOS：无 Landlock/seccomp 内核原语，走应用层 CommandFilter
        return "app-layer"

    @staticmethod
    def _unshare_available() -> bool:
        if not shutil.which("unshare"):
            return False
        try:
            r = subprocess.run(
                ["unshare", "-n", "true"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            )
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    async def run(self, req: ExecRequest) -> ExecResult:
        verdict = self._filter.check(req.cmd, req.profile, cwd=req.cwd)
        if verdict.blocked:
            return ExecResult(ok=False, output="", error=verdict.reason, returncode=-1, sandbox=self.name)
        prefix, _ = self._shell
        base_argv = [*prefix, req.cmd]
        if self._isolation == "linux-kernel" and req.profile != SandboxProfile.DANGER_FULL:
            # 最佳努力断网：无网命名空间（零依赖），失败自动降级（_choose_isolation 已探活，
            # 仍再包一层保护防止极端情况）
            argv = ["unshare", "-n", *base_argv]
            try:
                return await _run_subprocess(argv, cwd=req.cwd, env=req.env, timeout=req.timeout, label=self.name)
            except OSError:
                _log.warning("unshare 执行失败，降级为进程级执行")
        return await _run_subprocess(base_argv, cwd=req.cwd, env=req.env, timeout=req.timeout, label=self.name)


class DockerExecutor:
    """Docker 执行器：一次性容器，profile 映射为挂载与网络。跨平台一致的强隔离。"""

    name = "docker"

    def __init__(
        self,
        *,
        workspace: Path,
        profile: SandboxProfile = SandboxProfile.WORKSPACE_WRITE,
        image: str = "ubuntu:latest",
    ) -> None:
        self._workspace = Path(workspace)
        self._profile = profile
        self._image = image
        self._filter = CommandFilter(workspace=self._workspace)

    async def run(self, req: ExecRequest) -> ExecResult:
        # Docker 已提供 OS 级隔离，这里不再叠应用层 CommandFilter（避免双重拦截）；
        # 仅做 profile → 挂载/网络的映射。
        net = "none" if req.profile != SandboxProfile.DANGER_FULL else "host"
        ro = "ro" if req.profile == SandboxProfile.READ_ONLY else "rw"
        mount = f"{self._workspace}:/work:{ro}"
        argv = [
            "docker", "run", "--rm", "-w", "/work",
            "-v", mount, "--network", net,
            self._image, "/bin/sh", "-c", req.cmd,
        ]
        return await _run_subprocess(argv, cwd=req.cwd, env=req.env, timeout=req.timeout, label=self.name)


class ExternalExecutor:
    """直通执行器：不做进程内隔离，由外层环境（容器/CI/WSL）负责安全（对应 Codex externalSandbox）。"""

    name = "external"

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        profile: SandboxProfile = SandboxProfile.WORKSPACE_WRITE,
    ) -> None:
        self._shell = _resolve_shell()

    async def run(self, req: ExecRequest) -> ExecResult:
        prefix, _ = self._shell
        argv = [*prefix, req.cmd]
        return await _run_subprocess(argv, cwd=req.cwd, env=req.env, timeout=req.timeout, label=self.name)


class FakeExecutor:
    """测试执行器：记录全部 ``ExecRequest``，返回脚本化 ``ExecResult``（不真跑，确定性）。"""

    name = "fake"

    def __init__(
        self,
        *,
        script: "list[ExecResult] | Callable[[ExecRequest], ExecResult] | None" = None,
    ) -> None:
        self.requests: list[ExecRequest] = []
        self._script = script

    async def run(self, req: ExecRequest) -> ExecResult:
        self.requests.append(req)
        if callable(self._script):
            return self._script(req)
        if isinstance(self._script, list) and self._script:
            return self._script.pop(0)
        return ExecResult(ok=True, output="", error=None, returncode=0, sandbox=self.name)


# --------------------------------------------------------------------------- #
# 工厂 + 模块级注入点
# --------------------------------------------------------------------------- #
def build_executor(
    mode: str,
    *,
    workspace: Path,
    profile: SandboxProfile = SandboxProfile.WORKSPACE_WRITE,
) -> Executor:
    """按 ``mode`` 构造执行器。``mode ∈ {"local","docker","external"}``（对应 settings.sandbox_mode）。"""
    workspace = Path(workspace)
    if mode == "local":
        return LocalExecutor(workspace=workspace, profile=profile)
    if mode == "docker":
        return DockerExecutor(workspace=workspace, profile=profile)
    if mode == "external":
        return ExternalExecutor(workspace=workspace, profile=profile)
    raise ValueError(f"unknown sandbox mode: {mode!r} (expected local/docker/external)")


# 模块级当前执行器（注入点）：测试用 set_executor(FakeExecutor(...)) 替换；bash 工具经 get_executor() 取。
_EXECUTOR: Executor | None = None


def set_executor(executor: Executor | None) -> None:
    """注入/清除当前执行器（测试用）。传 None 恢复默认工厂。"""
    global _EXECUTOR
    _EXECUTOR = executor


def get_executor() -> Executor:
    """返回当前执行器。未注入时按默认（local + workspace-write + cwd）构造。

    M2.3/2.4 将改为读取 ``Settings.sandbox_mode`` / ``sandbox_profile``；此处保持默认工厂，
    使 M2.1 自包含、可被测试确定性驱动，不依赖尚未落地的配置字段。
    """
    if _EXECUTOR is not None:
        return _EXECUTOR
    return build_executor(
        "local", workspace=Path.cwd(), profile=SandboxProfile.WORKSPACE_WRITE
    )
