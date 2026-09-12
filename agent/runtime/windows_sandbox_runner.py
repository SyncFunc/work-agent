"""Codex 式 Windows restricted-token runner（由沙箱用户执行）。

父进程用 ``CreateProcessWithLogonW`` 以 ``CodexSandboxOffline`` / ``CodexSandboxOnline``
身份启动本脚本；本脚本从 stdin 读取 JSON payload，从自身 token 派生 write-restricted
token（restricted SID list = ``[Everyone, LogonSession, sandbox-write]``），再用
``CreateProcessAsUserW`` 启动真实命令。这样把「跨用户登录」与「最终受限 spawn」分离，
与 Codex ``command_runner/win.rs`` 的分层一致，且普通用户进程无需 ``SE_ASSIGNPRIMARYTOKEN``。

退出码：子进程退出码；runner 自身失败时打印 ``[sandbox-runner]`` 前缀错误并返回 253。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import subprocess
import sys

_ADVAPI32: ctypes.WinDLL | None = None
_KERNEL32: ctypes.WinDLL | None = None

_TOKEN_QUERY = 0x0008
_TOKEN_DUPLICATE = 0x0002
_TOKEN_ASSIGN_PRIMARY = 0x0001
_TOKEN_ADJUST_DEFAULT = 0x0080
_TOKEN_ADJUST_SESSIONID = 0x0100
_TOKEN_ALL_ACCESS = (
    _TOKEN_ASSIGN_PRIMARY
    | _TOKEN_DUPLICATE
    | _TOKEN_QUERY
    | _TOKEN_ADJUST_DEFAULT
    | _TOKEN_ADJUST_SESSIONID
)

_DISABLE_MAX_PRIVILEGE = 0x00000001
_LUA_TOKEN = 0x00000004
_WRITE_RESTRICTED = 0x00000008

_TOKEN_GROUPS = 2
_SE_GROUP_LOGON_ID = 0x00000020

_CREATE_NO_WINDOW = 0x08000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_STARTF_USESTDHANDLES = 0x00000100
_INFINITE = 0xFFFFFFFF
_STD_OUTPUT_HANDLE = -11
_STD_ERROR_HANDLE = -12


class _SID_AND_ATTRIBUTES(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("Sid", ctypes.c_void_p),
        ("Attributes", wintypes.DWORD),
    ]


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


def _bind() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    global _ADVAPI32, _KERNEL32
    if _ADVAPI32 is not None and _KERNEL32 is not None:
        return _ADVAPI32, _KERNEL32

    adv = ctypes.WinDLL("advapi32.dll", use_last_error=True)
    ker = ctypes.WinDLL("kernel32.dll", use_last_error=True)

    ker.GetCurrentProcess.argtypes = []
    ker.GetCurrentProcess.restype = wintypes.HANDLE

    adv.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    adv.OpenProcessToken.restype = wintypes.BOOL

    adv.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    adv.GetTokenInformation.restype = wintypes.BOOL

    adv.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    adv.ConvertStringSidToSidW.restype = wintypes.BOOL

    adv.CreateRestrictedToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SID_AND_ATTRIBUTES),
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_SID_AND_ATTRIBUTES),
        ctypes.POINTER(wintypes.HANDLE),
    ]
    adv.CreateRestrictedToken.restype = wintypes.BOOL

    adv.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFO),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    adv.CreateProcessAsUserW.restype = wintypes.BOOL

    ker.GetStdHandle.argtypes = [wintypes.DWORD]
    ker.GetStdHandle.restype = wintypes.HANDLE

    ker.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    ker.WaitForSingleObject.restype = wintypes.DWORD

    ker.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    ker.GetExitCodeProcess.restype = wintypes.BOOL

    ker.CloseHandle.argtypes = [wintypes.HANDLE]
    ker.CloseHandle.restype = wintypes.BOOL

    ker.LocalFree.argtypes = [ctypes.c_void_p]
    ker.LocalFree.restype = ctypes.c_void_p

    _ADVAPI32, _KERNEL32 = adv, ker
    return adv, ker


def _logon_sid(adv: ctypes.WinDLL, ker: ctypes.WinDLL) -> tuple[int, ctypes.Array]:
    """返回当前登录会话 SID 指针及承载它的缓冲区。

    GetTokenInformation 把 SID 复制到调用方缓冲区，缓冲区析构后指针即悬空；
    因此调用方必须持有返回的 buffer，直到 CreateRestrictedToken 返回。
    """
    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(ker.GetCurrentProcess(), _TOKEN_ALL_ACCESS, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buf = ctypes.create_string_buffer(4096)
        needed = wintypes.DWORD()
        ok = adv.GetTokenInformation(
            token,
            _TOKEN_GROUPS,
            buf,
            ctypes.sizeof(buf),
            ctypes.byref(needed),
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        raw = buf.raw[: int(needed.value)]
        count = int.from_bytes(raw[:4], "little")
        entry_size = ctypes.sizeof(_SID_AND_ATTRIBUTES)
        alignment = ctypes.alignment(_SID_AND_ATTRIBUTES)
        header_size = ((4 + alignment - 1) // alignment) * alignment
        for i in range(count):
            off = header_size + i * entry_size
            if off + 12 > len(raw):
                break
            sid_ptr = int.from_bytes(raw[off : off + 8], "little")
            attrs = int.from_bytes(raw[off + 8 : off + 12], "little")
            if attrs & _SE_GROUP_LOGON_ID:
                return sid_ptr, buf
        raise RuntimeError("token 中未找到 Logon SID")
    finally:
        ker.CloseHandle(token)


def _convert_sid(adv: ctypes.WinDLL, ker: ctypes.WinDLL, sid_str: str) -> int:
    p = ctypes.c_void_p()
    if not adv.ConvertStringSidToSidW(sid_str, ctypes.byref(p)):
        raise ctypes.WinError(ctypes.get_last_error())
    if p.value is None:
        raise RuntimeError("ConvertStringSidToSidW 返回空 SID")
    return int(p.value)


def _build_environment_block(env: dict[str, str]) -> tuple[ctypes.c_void_p, ctypes.Array]:
    items = [f"{k}={v}" for k, v in env.items()]
    data = ("\0".join(items) + "\0\0").encode("utf-16-le")
    buf = ctypes.create_string_buffer(data)
    return ctypes.cast(buf, ctypes.c_void_p), buf


def _create_restricted_token(
    adv: ctypes.WinDLL, ker: ctypes.WinDLL, sandbox_sid: str
) -> wintypes.HANDLE:
    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(ker.GetCurrentProcess(), _TOKEN_ALL_ACCESS, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())

    everyone_ptr = _convert_sid(adv, ker, "S-1-1-0")
    logon_ptr, logon_buf = _logon_sid(adv, ker)
    synthetic_ptr = _convert_sid(adv, ker, sandbox_sid)
    sids = (_SID_AND_ATTRIBUTES * 3)()
    sids[0].Sid = ctypes.c_void_p(everyone_ptr)
    sids[1].Sid = ctypes.c_void_p(logon_ptr)
    sids[2].Sid = ctypes.c_void_p(synthetic_ptr)

    restricted = wintypes.HANDLE()
    try:
        ok = adv.CreateRestrictedToken(
            token,
            _DISABLE_MAX_PRIVILEGE | _LUA_TOKEN | _WRITE_RESTRICTED,
            0,
            None,
            0,
            None,
            3,
            sids,
            ctypes.byref(restricted),
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return restricted
    finally:
        ker.LocalFree(ctypes.c_void_p(everyone_ptr))
        ker.LocalFree(ctypes.c_void_p(synthetic_ptr))
        ker.CloseHandle(token)


def _kill_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _spawn_and_wait(
    adv: ctypes.WinDLL, ker: ctypes.WinDLL, restricted: wintypes.HANDLE, payload: dict
) -> int:
    si = _STARTUPINFO()
    si.cb = ctypes.sizeof(_STARTUPINFO)
    si.dwFlags = _STARTF_USESTDHANDLES
    si.hStdOutput = ker.GetStdHandle(_STD_OUTPUT_HANDLE)
    si.hStdError = ker.GetStdHandle(_STD_ERROR_HANDLE)
    si.hStdInput = wintypes.HANDLE(0)

    pi = _PROCESS_INFORMATION()
    cmd_buf = ctypes.create_unicode_buffer(payload["command"])
    cwd_buf = ctypes.create_unicode_buffer(payload.get("cwd") or os.getcwd())
    env_block, env_buf = _build_environment_block(payload.get("env") or {})

    ok = adv.CreateProcessAsUserW(
        restricted,
        None,
        cmd_buf,
        None,
        None,
        True,
        _CREATE_NO_WINDOW | _CREATE_UNICODE_ENVIRONMENT,
        env_block,
        cwd_buf,
        ctypes.byref(si),
        ctypes.byref(pi),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())

    timeout = int(payload.get("timeout") or 0)
    wait_ms = _INFINITE if timeout <= 0 else timeout * 1000
    res = ker.WaitForSingleObject(pi.hProcess, wait_ms)
    if res != 0:
        _kill_tree(int(pi.dwProcessId))
        ker.WaitForSingleObject(pi.hProcess, 30_000)
    code = wintypes.DWORD()
    ker.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    ker.CloseHandle(pi.hThread)
    ker.CloseHandle(pi.hProcess)
    return int(code.value) if res == 0 else 124


def main() -> int:
    try:
        payload_bytes = sys.stdin.buffer.read()
        try:
            os.close(0)
        except OSError:
            pass
        payload = json.loads(payload_bytes.decode("utf-8"))
        adv, ker = _bind()
        restricted = _create_restricted_token(adv, ker, payload["sandbox_sid"])
        try:
            return _spawn_and_wait(adv, ker, restricted, payload)
        finally:
            ker.CloseHandle(restricted)
    except Exception as e:  # noqa: BLE001 - runner 必须把任何失败转成非零退出码
        print(f"[sandbox-runner] {type(e).__name__}: {e}", file=sys.stderr)
        return 253


if __name__ == "__main__":
    sys.exit(main())
