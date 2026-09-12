"""Windows 沙箱 spawner（对齐 Codex elevated sandbox 方案）。

核心链路（与 Codex ``windows-sandbox-rs`` 对齐）：

1. 管理员 setup 创建 ``CodexSandboxOffline`` / ``CodexSandboxOnline`` 两个本地用户、
   合成 SID ``sandbox-write``、DPAPI 加密凭据、按用户 SID 绑定的防火墙规则。
2. 运行时父进程用 ``CreateProcessWithLogonW`` 以沙箱用户身份启动
   ``windows_sandbox_runner.py``（无需提权）。
3. runner 在沙箱用户身份内从自身 token 派生 **write-restricted token**
   （``DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED``，restricted SID list =
   ``[Everyone, LogonSession, sandbox-write]``），再以 ``CreateProcessAsUserW``
   启动真实命令。
4. 写隔离由 restricted SID + ACL 强制：工作区/``writable_roots`` 授予
   ``sandbox-write`` Write/Execute/Delete；``.git`` / ``.codex`` / ``.agents`` /
   ``.agent`` 对该 SID 显式 Deny Write。

设计依据：``knowledge/appcontainer-sandbox-design.md`` + 教学文档
``docs/codex-windows-sandbox.md``。
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wintypes
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.runtime.sandbox import ExecResult

_log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 常量 / 可用性
# --------------------------------------------------------------------------- #
_IS_WIN32 = sys.platform == "win32"
_WINDOWS_AVAILABLE = False
_advapi32: Any = None
_kernel32: Any = None
_crypt32: Any = None

_LOGON_WITH_PROFILE = 0x00000001
_CREATE_NO_WINDOW = 0x08000000
_STARTF_USESTDHANDLES = 0x00000100
_WAIT_OBJECT_0 = 0x00000000

# 默认沙箱用户名（与 scripts/windows_sandbox_setup.ps1 对齐）
DEFAULT_GROUP = "CodexSandboxUsers"
DEFAULT_OFFLINE_USER = "CodexSandboxOffline"
DEFAULT_ONLINE_USER = "CodexSandboxOnline"

# 工作区可写区域内必须保持只读的默认目录（对齐 Codex + 本项目 .agent）
DEFAULT_DENY_NAMES = (".git", ".codex", ".agents", ".agent")

_DPAPI_ENTROPY = b"work-agent-sandbox-v1"


@dataclass
class SandboxCredentials:
    """setup 写入、运行时 DPAPI 解密出的沙箱凭据。"""

    offline_username: str
    online_username: str
    password: str


@dataclass
class WindowsSandboxSpec:
    """一次沙箱执行的配置（纯数据，便于单测）。"""

    username: str  # 沙箱用户（Offline=断网 / Online=联网）
    group_name: str
    workspace: Path
    profile: str
    write_roots: list[Path]  # 授写路径（通常仅 workspace + 配置的 writable_roots）
    deny_paths: list[Path]  # 工作区内对 sandbox-write SID 显式 Deny Write 的路径


class SandboxUserUnavailable(RuntimeError):
    """沙箱未就绪（未运行管理员 setup）或无法启动命令。"""


# --------------------------------------------------------------------------- #
# ctypes 绑定（仅 Windows）
# --------------------------------------------------------------------------- #
if _IS_WIN32:
    try:
        _advapi32 = ctypes.WinDLL("advapi32.dll", use_last_error=True)
        _kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        _crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
        _WINDOWS_AVAILABLE = True
    except OSError:
        _log.warning("无法加载 Windows DLL，Windows 沙箱不可用")
        _WINDOWS_AVAILABLE = False


if _WINDOWS_AVAILABLE:

    class _STARTUPINFO(ctypes.Structure):  # noqa: N801
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):  # noqa: N801
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class _DATA_BLOB(ctypes.Structure):  # noqa: N801
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.c_void_p),
        ]

    # CreateProcessWithLogonW (advapi32)
    _advapi32.CreateProcessWithLogonW.argtypes = [
        wintypes.LPCWSTR,  # lpUsername
        wintypes.LPCWSTR,  # lpDomain
        wintypes.LPCWSTR,  # lpPassword
        wintypes.DWORD,  # dwLogonFlags
        wintypes.LPCWSTR,  # lpApplicationName
        wintypes.LPWSTR,  # lpCommandLine
        wintypes.DWORD,  # dwCreationFlags
        ctypes.c_void_p,  # lpEnvironment
        wintypes.LPCWSTR,  # lpCurrentDirectory
        ctypes.POINTER(_STARTUPINFO),  # lpStartupInfo
        ctypes.POINTER(_PROCESS_INFORMATION),  # lpProcessInformation
    ]
    _advapi32.CreateProcessWithLogonW.restype = wintypes.BOOL

    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD

    _kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    # DPAPI (crypt32)
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL

    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL


# --------------------------------------------------------------------------- #
# 公开工具（纯逻辑，可单测）
# --------------------------------------------------------------------------- #
def available() -> bool:
    """Windows + DLL 是否可用。"""
    return _WINDOWS_AVAILABLE


def profile_username(profile_value: str) -> str:
    """profile → 沙箱用户名。前两档用 Offline（断网）；danger-full 用 Online。"""
    if profile_value == "danger-full":
        return DEFAULT_ONLINE_USER
    return DEFAULT_OFFLINE_USER


def default_deny_paths(root: Path) -> list[Path]:
    """工作区可写区域内必须只读的默认路径（.git / .codex / .agents / .agent）。"""
    return [Path(root) / name for name in DEFAULT_DENY_NAMES]


def build_spec(
    profile_value: str,
    workspace: Path,
    *,
    write_roots: list[Path] | None = None,
    deny_paths: list[Path] | None = None,
) -> WindowsSandboxSpec:
    """构造某 profile 的沙箱规格。前两档断网；danger-full 联网（调用方通常裸跑）。"""
    workspace = Path(workspace)
    roots = [workspace, *(Path(p) for p in (write_roots or []))]
    deny: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for dp in default_deny_paths(root):
            if dp not in seen:
                deny.append(dp)
                seen.add(dp)
    for dp in deny_paths or []:
        p = Path(dp)
        if p not in seen:
            deny.append(p)
            seen.add(p)
    return WindowsSandboxSpec(
        username=profile_username(profile_value),
        group_name=DEFAULT_GROUP,
        workspace=workspace,
        profile=profile_value,
        write_roots=roots,
        deny_paths=deny,
    )


# --------------------------------------------------------------------------- #
# 凭据 / 合成 SID 文件
# --------------------------------------------------------------------------- #
def cred_file(workspace: Path) -> Path:
    """DPAPI 凭据文件路径（setup 脚本写入）。"""
    return Path(workspace) / ".agent" / "sandbox_creds.bin"


def legacy_cred_file(workspace: Path) -> Path:
    """旧版明文凭据路径（迁移/兼容读取；新 setup 不再写入）。"""
    return Path(workspace) / ".agent" / "sandbox_user.txt"


def sandbox_sid_file(workspace: Path) -> Path:
    """合成 SID 文件路径（setup 脚本写入，仅当前用户可读）。"""
    return Path(workspace) / ".agent" / "sandbox-write.sid"


def read_sandbox_sid(workspace: Path) -> str:
    """读取 sandbox-write 合成 SID（S-1-5-21-...）。"""
    f = sandbox_sid_file(workspace)
    if not f.is_file():
        raise SandboxUserUnavailable(
            f"沙箱合成 SID 文件不存在: {f}。请以管理员运行 "
            "scripts/windows_sandbox_setup.ps1 完成初始化。"
        )
    sid = f.read_text(encoding="ascii").strip()
    if not sid.startswith("S-1-"):
        raise SandboxUserUnavailable(f"合成 SID 格式错误: {f}")
    return sid


def protect_bytes(data: bytes, entropy: bytes = _DPAPI_ENTROPY) -> bytes:
    """DPAPI 加密（CurrentUser 范围）。仅 Windows。"""
    if not _IS_WIN32 or not _WINDOWS_AVAILABLE:
        raise SandboxUserUnavailable("DPAPI 仅 Windows 可用")
    in_blob, in_buf = _make_blob(data)
    entropy_blob: _DATA_BLOB | None = None
    entropy_buf: ctypes.Array | None = None
    if entropy:
        entropy_blob, entropy_buf = _make_blob(entropy)
    out_blob = _DATA_BLOB()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(entropy_blob) if entropy_blob is not None else None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _kernel32.LocalFree(ctypes.c_void_p(out_blob.pbData))


def unprotect_bytes(data: bytes, entropy: bytes = _DPAPI_ENTROPY) -> bytes:
    """DPAPI 解密（CurrentUser 范围）。仅 Windows。"""
    if not _IS_WIN32 or not _WINDOWS_AVAILABLE:
        raise SandboxUserUnavailable("DPAPI 仅 Windows 可用")
    in_blob, in_buf = _make_blob(data)
    entropy_blob: _DATA_BLOB | None = None
    entropy_buf: ctypes.Array | None = None
    if entropy:
        entropy_blob, entropy_buf = _make_blob(entropy)
    out_blob = _DATA_BLOB()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(entropy_blob) if entropy_blob is not None else None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _kernel32.LocalFree(ctypes.c_void_p(out_blob.pbData))


def _make_blob(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.c_void_p).value)
    return blob, buf


def read_credentials(workspace: Path) -> SandboxCredentials:
    """读取沙箱凭据：优先 DPAPI 密文，旧版明文仅作迁移兜底。"""
    cf = cred_file(workspace)
    if cf.is_file():
        try:
            plain = unprotect_bytes(cf.read_bytes())
            data = json.loads(plain.decode("utf-8"))
            return SandboxCredentials(
                offline_username=str(data["offline_username"]),
                online_username=str(data["online_username"]),
                password=str(data["password"]),
            )
        except Exception as e:  # noqa: BLE001 - 统一转成可读的沙箱未就绪错误
            raise SandboxUserUnavailable(f"DPAPI 凭据解密失败: {e}") from e

    legacy = legacy_cred_file(workspace)
    if legacy.is_file():
        _log.warning("检测到旧版明文凭据 %s，请重跑 setup 升级为 DPAPI 密文", legacy)
        lines = legacy.read_text(encoding="utf-8").splitlines()
        if len(lines) < 2 or not lines[1]:
            raise SandboxUserUnavailable(f"凭据文件格式错误: {legacy}")
        username = lines[0].strip()
        return SandboxCredentials(
            offline_username=username,
            online_username=username,
            password=lines[1].strip(),
        )

    raise SandboxUserUnavailable(
        f"沙箱凭据文件不存在: {cf}。请以管理员运行 scripts/windows_sandbox_setup.ps1 完成初始化。"
    )


# --------------------------------------------------------------------------- #
# ACL 管理（sandbox-write SID）
# --------------------------------------------------------------------------- #
def _icacls(argv: list[str]) -> None:
    """执行一条 icacls 命令；失败抛 SandboxUserUnavailable。"""
    icacls = shutil.which("icacls")
    if not icacls:
        raise SandboxUserUnavailable("icacls 不可用，无法配置沙箱 ACL")
    r = subprocess.run(
        [icacls, *argv],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if r.returncode != 0:
        raise SandboxUserUnavailable(f"icacls 失败({argv}): {r.stdout.strip()} {r.stderr.strip()}")


def _grant_argv(path: Path, principal: str, rights: str, *, recursive: bool = True) -> list[str]:
    argv = [str(path), "/grant", f"{principal}:{rights}"]
    if recursive:
        argv.append("/T")
    return argv


def _deny_argv(path: Path, principal: str, rights: str, *, recursive: bool = True) -> list[str]:
    argv = [str(path), "/deny", f"{principal}:{rights}"]
    if recursive:
        argv.append("/T")
    return argv


def _cleanup_legacy_user_aces(root: Path) -> None:
    """移除旧方案授予沙箱用户的直接写 ACE（迁移到 synthetic SID 后不再使用）。"""
    if not _IS_WIN32:
        return
    for user in (DEFAULT_OFFLINE_USER, DEFAULT_ONLINE_USER):
        if user_exists(user):
            try:
                _icacls([str(root), "/remove:g", user, "/T"])
            except SandboxUserUnavailable as e:
                _log.warning("清理旧沙箱用户 ACE 失败（忽略）: %s", e)


def ensure_read_acl(workspace: Path) -> None:
    """给沙箱用户组授予 workspace Read/Execute（递归），保证命令可读、可执行。"""
    if not _IS_WIN32:
        return
    _cleanup_legacy_user_aces(workspace)
    _icacls(_grant_argv(workspace, DEFAULT_GROUP, "(OI)(CI)(RX)"))


def ensure_write_acl(spec: WindowsSandboxSpec) -> None:
    """授予 sandbox-write SID 对 write_roots 的 Write/Execute/Delete，并对 deny_paths 拒绝。"""
    if not _IS_WIN32:
        return
    sid = read_sandbox_sid(spec.workspace)
    for root in spec.write_roots:
        if not root.exists():
            _log.warning("writable root 不存在，跳过 ACL: %s", root)
            continue
        _icacls(_grant_argv(root, f"*{sid}", "(OI)(CI)(W,D,AD,DC,X)"))
        _icacls(_grant_argv(root, DEFAULT_GROUP, "(OI)(CI)(RX)"))
    for dp in spec.deny_paths:
        if dp.exists():
            _icacls(_deny_argv(dp, f"*{sid}", "(OI)(CI)(W,D,AD,DC)"))


def ensure_sandbox_acl(spec: WindowsSandboxSpec) -> None:
    """按 profile 准备 ACL：read-only 只补读；workspace-write 再授合成 SID 写。"""
    if not _IS_WIN32:
        return
    ensure_read_acl(spec.workspace)
    if spec.profile == "workspace-write":
        ensure_write_acl(spec)


# --------------------------------------------------------------------------- #
# 状态 / 可用性
# --------------------------------------------------------------------------- #
def user_exists(username: str) -> bool:
    """沙箱用户是否存在（net user 查询）。"""
    if not _IS_WIN32:
        return False
    try:
        r = subprocess.run(
            ["net", "user", username],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


_usable_cache: dict[str, bool] = {}


def usable(
    workspace: Path,
    profile_value: str = "workspace-write",
    shell_prefix: list[str] | None = None,
) -> bool:
    """沙箱是否真正可用：用户存在 + 凭据/SID 可读 + 能启动一条受限命令。"""
    if not available():
        return False
    key = f"{workspace}:{profile_value}"
    if key in _usable_cache:
        return _usable_cache[key]
    ok = _probe_usable(Path(workspace), profile_value, shell_prefix)
    _usable_cache[key] = ok
    return ok


def _probe_usable(
    workspace: Path,
    profile_value: str,
    shell_prefix: list[str] | None = None,
) -> bool:
    """探测：用户存在、凭据/SID 可读、ACL 就绪、以沙箱用户能启动真实命令。"""
    if not user_exists(profile_username(profile_value)):
        _log.debug("沙箱用户 %s 不存在", profile_username(profile_value))
        return False
    try:
        read_credentials(workspace)
        read_sandbox_sid(workspace)
        sp = WindowsRestrictedUserSpawner(workspace=workspace, profile_value=profile_value)
        sp.prepare()
        r = sp._run_sync(  # noqa: SLF001 - 探测用同步入口
            "echo sandbox-probe-ok",
            cwd=workspace,
            env={},
            timeout=15,
            shell_prefix=shell_prefix or ["cmd.exe", "/c"],
        )
        return bool(r.ok)
    except Exception:  # noqa: BLE001
        _log.debug("Windows 沙箱探测失败", exc_info=True)
        return False


# --------------------------------------------------------------------------- #
# Spawner
# --------------------------------------------------------------------------- #
class WindowsRestrictedUserSpawner:
    """以受限沙箱用户身份执行命令（runner 中转 + write-restricted token）。"""

    name = "windows-restricted-user"

    def __init__(
        self,
        *,
        workspace: Path,
        profile_value: str = "workspace-write",
        write_roots: list[Path] | None = None,
        deny_paths: list[Path] | None = None,
    ) -> None:
        self._workspace = Path(workspace)
        self._profile_value = profile_value
        self._spec = build_spec(
            profile_value,
            self._workspace,
            write_roots=write_roots,
            deny_paths=deny_paths,
        )
        self._prepared = False

    def prepare(self) -> None:
        """运行时只做就绪检查；ACL 由管理员 setup 一次性配置。

        ACL 写入需要管理员权限，普通 Agent 进程在运行时不应尝试 icacls。
        """
        if not _IS_WIN32:
            return
        read_credentials(self._workspace)
        read_sandbox_sid(self._workspace)
        self._prepared = True

    def grant_workspace_write(self) -> None:
        """兼容旧 API：等价于 workspace-write 的就绪检查（ACL 由 setup 配置）。"""
        self.prepare()

    def _build_payload(
        self,
        cmd: str,
        *,
        cwd: Path,
        env: dict[str, str],
        shell_prefix: list[str],
        timeout: int,
    ) -> bytes:
        full_env = dict(os.environ)
        full_env["LANG"] = "C.UTF-8"
        full_env["LC_ALL"] = "C.UTF-8"
        full_env.update(env or {})
        payload = {
            "command": subprocess.list2cmdline([*shell_prefix, cmd]),
            "cwd": str(cwd),
            "env": full_env,
            "sandbox_sid": read_sandbox_sid(self._workspace),
            "profile": self._profile_value,
            "timeout": timeout,
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _runner_argv(self) -> list[str]:
        exe = Path(sys.executable)
        # 优先用 venv 的 base 解释器：runner 只用标准库，base 解释器通常对沙箱用户可读可执行。
        if sys.prefix != sys.base_prefix:
            base_exe = Path(sys.base_prefix) / "python.exe"
            if base_exe.is_file():
                exe = base_exe
        script = Path(__file__).resolve().parent / "windows_sandbox_runner.py"
        return [str(exe), "-X", "utf8", str(script)]

    def spawn_handles(
        self,
        cmd: str,
        *,
        cwd: Path,
        env: dict[str, str],
        shell_prefix: list[str],
        stdout_write: int,
        stderr_write: int,
        timeout: int = 0,
    ) -> tuple[int, int]:
        """以沙箱用户身份启动 runner，返回 (runner_pid, hProcess)。"""
        if not _IS_WIN32 or not _WINDOWS_AVAILABLE:
            raise SandboxUserUnavailable("Windows 沙箱不可用（非 Windows）")
        creds = read_credentials(self._workspace)
        username = self._spec.username
        if not user_exists(username):
            raise SandboxUserUnavailable(
                f"沙箱用户 {username} 不存在，请运行 scripts/windows_sandbox_setup.ps1"
            )
        payload = self._build_payload(
            cmd, cwd=cwd, env=env, shell_prefix=shell_prefix, timeout=timeout
        )

        payload_r, payload_w = os.pipe()
        try:
            try:
                os.set_handle_inheritable(payload_r, True)
            except OSError:
                pass
            runner_cmdline = subprocess.list2cmdline(self._runner_argv())
            env_block, env_buf = _build_environment_block(self._runner_env())

            si = _STARTUPINFO()
            si.cb = ctypes.sizeof(_STARTUPINFO)
            si.dwFlags = _STARTF_USESTDHANDLES
            si.hStdInput = wintypes.HANDLE(payload_r)
            si.hStdOutput = wintypes.HANDLE(stdout_write)
            si.hStdError = wintypes.HANDLE(stderr_write)
            pi = _PROCESS_INFORMATION()
            ok = _advapi32.CreateProcessWithLogonW(
                username,
                None,  # local machine
                creds.password,
                _LOGON_WITH_PROFILE,
                None,
                runner_cmdline,
                _CREATE_NO_WINDOW,
                env_block,
                str(cwd),
                ctypes.byref(si),
                ctypes.byref(pi),
            )
            if not ok:
                err = ctypes.get_last_error()
                _log.warning("CreateProcessWithLogonW 失败，error=%s", err)
                raise SandboxUserUnavailable(f"CreateProcessWithLogonW 失败，error={err}")
            try:
                with os.fdopen(payload_w, "wb") as f:
                    f.write(payload)
                payload_w = -1
            except OSError as e:
                # runner 已提前退出（如解释器不可读），错误会在 stderr/退出码体现。
                _log.warning("runner payload 写入失败: %s", e)
            _kernel32.CloseHandle(pi.hThread)
            return int(pi.dwProcessId), int(pi.hProcess)
        finally:
            os.close(payload_r)
            if payload_w >= 0:
                try:
                    os.close(payload_w)
                except OSError:
                    pass

    @staticmethod
    def _runner_env() -> dict[str, str]:
        env = dict(os.environ)
        env["LANG"] = "C.UTF-8"
        env["LC_ALL"] = "C.UTF-8"
        return env

    async def run_async(
        self, cmd: str, *, cwd: Path, env: dict[str, str], timeout: int, shell_prefix: list[str]
    ) -> ExecResult:
        """以沙箱用户异步执行命令并收集输出。"""
        return await asyncio.to_thread(self._run_sync, cmd, cwd, env, timeout, shell_prefix)

    def _run_sync(
        self, cmd: str, cwd: Path, env: dict[str, str], timeout: int, shell_prefix: list[str]
    ) -> ExecResult:
        if not _IS_WIN32 or not _WINDOWS_AVAILABLE:
            return ExecResult(
                ok=False,
                output="",
                error="Windows 沙箱不可用（非 Windows）",
                returncode=-1,
                sandbox=self.name,
            )
        out_r, out_w = _create_pipe()
        err_r, err_w = _create_pipe()
        pid, hproc = 0, 0
        try:
            pid, hproc = self.spawn_handles(
                cmd,
                cwd=cwd,
                env=env,
                shell_prefix=shell_prefix,
                stdout_write=out_w,
                stderr_write=err_w,
                timeout=timeout,
            )
        except SandboxUserUnavailable as e:
            return ExecResult(ok=False, output="", error=str(e), returncode=-1, sandbox=self.name)
        finally:
            os.close(out_w)
            os.close(err_w)

        start = time.monotonic()
        deadline = start + timeout + 30
        rc = 0
        while True:
            res = _kernel32.WaitForSingleObject(hproc, 100)
            if res == _WAIT_OBJECT_0:
                code = wintypes.DWORD()
                _kernel32.GetExitCodeProcess(hproc, ctypes.byref(code))
                rc = int(code.value)
                break
            if time.monotonic() > deadline:
                _kill_process_tree(pid)
                _kernel32.CloseHandle(hproc)
                return ExecResult(
                    ok=False,
                    output="",
                    error=f"runner timed out after {timeout}s",
                    returncode=-1,
                    sandbox=self.name,
                )
        _kernel32.CloseHandle(hproc)

        out = _read_all(out_r)
        err = _read_all(err_r)
        text = _decode_bytes(out)
        err_text = _decode_bytes(err)
        if err_text:
            text = text + ("" if text.endswith("\n") else "\n") + f"[stderr]\n{err_text}"
        return ExecResult(
            ok=rc == 0,
            output=text,
            error=None if rc == 0 else f"exit code {rc}",
            returncode=rc,
            sandbox=self.name,
        )


# --------------------------------------------------------------------------- #
# 管道 / 环境辅助
# --------------------------------------------------------------------------- #
def _build_environment_block(env: dict[str, str]) -> tuple[ctypes.c_void_p, ctypes.Array]:
    """构造 UNICODE 环境块（双 NUL 结尾），返回 (指针, buffer) 供调用方保持 buffer 存活。"""
    items = [f"{k}={v}" for k, v in env.items()]
    data = ("\0".join(items) + "\0\0").encode("utf-16-le")
    buf = ctypes.create_string_buffer(data)
    return ctypes.cast(buf, ctypes.c_void_p), buf


def _create_pipe() -> tuple[int, int]:
    r, w = os.pipe()
    try:
        os.set_handle_inheritable(w, True)
    except OSError:
        pass
    os.set_blocking(r, False)
    return r, w


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        try:
            data = os.read(fd, 65536)
            if not data:
                break
            chunks.append(data)
        except BlockingIOError:
            if chunks:
                break
            time.sleep(0.01)
        except OSError:
            break
    try:
        os.close(fd)
    except OSError:
        pass
    return b"".join(chunks)


def _decode_bytes(b: bytes) -> str:
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("utf-8", errors="replace")


def _kill_process_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass
