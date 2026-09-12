"""Windows 受限沙箱（write-restricted token + synthetic SID）纯逻辑测试。

不真跑沙箱（需真实 Windows + 沙箱用户 + 管理员 setup），只验证：
- profile → 沙箱用户名映射（profile_username / build_spec）
- 凭据读取（DPAPI 优先 / 旧明文迁移 / 缺失报错）
- 合成 SID 文件读取
- ACL 命令构造（grant/deny argv）
- available() 在非 Windows 为 False
- LocalExecutor 的 isolation 选择逻辑（app-layer / restricted-user / auto）
- build_executor 透传 isolation / writable_roots

真沙箱冒烟仅 Windows 且标 slow（默认跳过）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent.runtime.sandbox import LocalExecutor, build_executor
from agent.runtime.sandbox_windows import (
    DEFAULT_GROUP,
    DEFAULT_OFFLINE_USER,
    DEFAULT_ONLINE_USER,
    SandboxUserUnavailable,
    WindowsRestrictedUserSpawner,
    _deny_argv,
    _grant_argv,
    available,
    build_spec,
    cred_file,
    default_deny_paths,
    legacy_cred_file,
    profile_username,
    protect_bytes,
    read_credentials,
    read_sandbox_sid,
    sandbox_sid_file,
    unprotect_bytes,
    usable,
)

_IS_WIN32 = sys.platform == "win32"


# --------------------------------------------------------------------------- #
# 纯逻辑映射
# --------------------------------------------------------------------------- #
def test_profile_username_restricted():
    assert profile_username("read-only") == DEFAULT_OFFLINE_USER
    assert profile_username("workspace-write") == DEFAULT_OFFLINE_USER


def test_profile_username_danger_full():
    assert profile_username("danger-full") == DEFAULT_ONLINE_USER


def test_build_spec_read_only():
    ws = Path("C:/agent-ws")
    spec = build_spec("read-only", ws)
    assert spec.username == DEFAULT_OFFLINE_USER
    assert spec.group_name == DEFAULT_GROUP
    assert spec.profile == "read-only"
    assert ws in spec.write_roots
    assert ws / ".git" in spec.deny_paths
    assert ws / ".agent" in spec.deny_paths


def test_build_spec_workspace_write():
    ws = Path("C:/agent-ws")
    spec = build_spec("workspace-write", ws)
    assert spec.username == DEFAULT_OFFLINE_USER
    assert spec.write_roots == [ws]


def test_build_spec_extra_writable_roots():
    ws = Path("C:/agent-ws")
    extra = Path("D:/build-cache")
    spec = build_spec("workspace-write", ws, write_roots=[extra])
    assert spec.write_roots == [ws, extra]
    assert extra / ".git" in spec.deny_paths


def test_build_spec_danger_full_uses_online():
    ws = Path("C:/agent-ws")
    spec = build_spec("danger-full", ws)
    assert spec.username == DEFAULT_ONLINE_USER


def test_default_deny_paths():
    ws = Path("C:/agent-ws")
    paths = default_deny_paths(ws)
    assert [p.name for p in paths] == [".git", ".codex", ".agents", ".agent"]


# --------------------------------------------------------------------------- #
# 凭据 / 合成 SID
# --------------------------------------------------------------------------- #
def test_cred_file_path(tmp_path: Path):
    assert cred_file(tmp_path) == tmp_path / ".agent" / "sandbox_creds.bin"


def test_legacy_cred_file_path(tmp_path: Path):
    assert legacy_cred_file(tmp_path) == tmp_path / ".agent" / "sandbox_user.txt"


def test_sandbox_sid_file_path(tmp_path: Path):
    assert sandbox_sid_file(tmp_path) == tmp_path / ".agent" / "sandbox-write.sid"


def test_read_credentials_missing(tmp_path: Path):
    with pytest.raises(SandboxUserUnavailable):
        read_credentials(tmp_path)


def test_read_credentials_legacy_migration(tmp_path: Path):
    cf = legacy_cred_file(tmp_path)
    cf.parent.mkdir(parents=True)
    cf.write_text("CodexSandboxOffline\nsecret-pw-123", encoding="utf-8")
    creds = read_credentials(tmp_path)
    assert creds.offline_username == "CodexSandboxOffline"
    assert creds.online_username == "CodexSandboxOffline"
    assert creds.password == "secret-pw-123"


def test_read_credentials_legacy_bad_format(tmp_path: Path):
    cf = legacy_cred_file(tmp_path)
    cf.parent.mkdir(parents=True)
    cf.write_text("only-one-line", encoding="utf-8")
    with pytest.raises(SandboxUserUnavailable):
        read_credentials(tmp_path)


def test_read_sandbox_sid_missing(tmp_path: Path):
    with pytest.raises(SandboxUserUnavailable):
        read_sandbox_sid(tmp_path)


def test_read_sandbox_sid_ok(tmp_path: Path):
    f = sandbox_sid_file(tmp_path)
    f.parent.mkdir(parents=True)
    f.write_text("S-1-5-21-111-222-333-444", encoding="ascii")
    assert read_sandbox_sid(tmp_path) == "S-1-5-21-111-222-333-444"


@pytest.mark.slow
@pytest.mark.skipif(
    not _IS_WIN32 or not available(), reason="需要 Windows + crypt32.dll + 用户 profile"
)
def test_dpapi_roundtrip():
    secret = b"CodexSandboxOffline\x00secret-pw-123"
    blob = protect_bytes(secret)
    assert blob != secret
    assert unprotect_bytes(blob) == secret


# --------------------------------------------------------------------------- #
# ACL 命令构造
# --------------------------------------------------------------------------- #
def test_grant_argv_uses_synthetic_sid():
    argv = _grant_argv(Path("C:/ws"), "*S-1-5-21-1-2-3-4", "(OI)(CI)(W,D,AD,DC,X)")
    assert Path(argv[0]) == Path("C:/ws")
    assert argv[1:] == [
        "/grant",
        "*S-1-5-21-1-2-3-4:(OI)(CI)(W,D,AD,DC,X)",
        "/T",
    ]


def test_deny_argv_uses_synthetic_sid():
    argv = _deny_argv(Path("C:/ws/.git"), "*S-1-5-21-1-2-3-4", "(OI)(CI)(W,D,AD,DC)")
    assert Path(argv[0]) == Path("C:/ws/.git")
    assert argv[1:] == [
        "/deny",
        "*S-1-5-21-1-2-3-4:(OI)(CI)(W,D,AD,DC)",
        "/T",
    ]


# --------------------------------------------------------------------------- #
# 可用性 + spawner 行为
# --------------------------------------------------------------------------- #
def test_available_false_on_non_windows():
    if not _IS_WIN32:
        assert available() is False


def test_spawner_spec_init(tmp_path: Path):
    extra = tmp_path / "build"
    sp = WindowsRestrictedUserSpawner(
        workspace=tmp_path,
        profile_value="workspace-write",
        write_roots=[extra],
    )
    assert sp._spec.username == DEFAULT_OFFLINE_USER
    assert sp._spec.write_roots == [tmp_path, extra]


def test_spawner_payload_includes_timeout(tmp_path: Path):
    f = sandbox_sid_file(tmp_path)
    f.parent.mkdir(parents=True)
    f.write_text("S-1-5-21-1-2-3-4", encoding="ascii")
    sp = WindowsRestrictedUserSpawner(workspace=tmp_path, profile_value="workspace-write")
    payload = json.loads(
        sp._build_payload(
            "echo hi",
            cwd=tmp_path,
            env={},
            shell_prefix=["cmd.exe", "/c"],
            timeout=9,
        )
    )
    assert payload["timeout"] == 9


def test_spawner_run_async_returns_unavailable_on_non_windows(tmp_path: Path):
    sp = WindowsRestrictedUserSpawner(workspace=tmp_path, profile_value="workspace-write")
    r = asyncio.run(
        sp.run_async("echo hi", cwd=tmp_path, env={}, timeout=5, shell_prefix=["cmd.exe", "/c"])
    )
    if not _IS_WIN32:
        assert not r.ok
        assert "沙箱" in (r.error or "")


# --------------------------------------------------------------------------- #
# LocalExecutor isolation 选择
# --------------------------------------------------------------------------- #
def test_local_executor_app_layer_forced(tmp_path: Path):
    ex = LocalExecutor(workspace=tmp_path, isolation="app-layer")
    assert ex._isolation == "app-layer"
    assert ex._windows_sandbox is None


def test_local_executor_restricted_user_forced_raises_off_windows(tmp_path: Path):
    if _IS_WIN32 and usable(tmp_path):
        pytest.skip("Windows + 沙箱用户真正可用时走真沙箱，不在此断言报错")
    with pytest.raises(RuntimeError):
        LocalExecutor(workspace=tmp_path, isolation="restricted-user")


def test_local_executor_auto_never_restricted_on_non_windows(tmp_path: Path):
    if not _IS_WIN32:
        ex = LocalExecutor(workspace=tmp_path, isolation="auto")
        assert ex._isolation != "windows-restricted-user"


def test_build_executor_passes_isolation_and_writable_roots(tmp_path: Path):
    ex = build_executor(
        "local",
        workspace=tmp_path,
        isolation="app-layer",
        writable_roots=["D:/build-cache"],
    )
    assert isinstance(ex, LocalExecutor)
    assert ex._isolation == "app-layer"
    assert ex._writable_roots == [Path("D:/build-cache")]


# --------------------------------------------------------------------------- #
# 真沙箱冒烟（仅 Windows 且 slow，默认跳过）
# --------------------------------------------------------------------------- #
@pytest.mark.slow
@pytest.mark.skipif(not _IS_WIN32 or not available(), reason="需要 Windows + 沙箱用户")
def test_real_restricted_user_echo(tmp_path: Path):
    sp = WindowsRestrictedUserSpawner(workspace=tmp_path, profile_value="workspace-write")
    r = asyncio.run(
        sp.run_async(
            "echo sandbox-ok", cwd=tmp_path, env={}, timeout=10, shell_prefix=["cmd.exe", "/c"]
        )
    )
    assert r.ok, r.error


@pytest.mark.slow
@pytest.mark.skipif(not _IS_WIN32 or not available(), reason="需要 Windows + 沙箱用户")
def test_real_restricted_user_cannot_write_outside(tmp_path: Path):
    """写隔离：restricted SID 未授权的位置应写失败。"""
    sp = WindowsRestrictedUserSpawner(workspace=tmp_path, profile_value="workspace-write")
    outside = tmp_path.parent / "outside.txt"
    r = asyncio.run(
        sp.run_async(
            f"echo x > {outside}",
            cwd=tmp_path,
            env={},
            timeout=10,
            shell_prefix=["cmd.exe", "/c"],
        )
    )
    assert not r.ok  # 应因权限被拒
