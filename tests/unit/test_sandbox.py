"""M2.1 沙盒执行层测试：build_executor / FakeExecutor / ExternalExecutor / LocalExecutor / CommandFilter。

不依赖真实 LLM / root / 网络：用 FakeExecutor 注入；External/Local 仅跑 ``echo`` 这类无害命令；
网络/越界写拦截由 CommandFilter（应用层，跨平台）验证，并断言拦截时不打印告警。
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

import pytest

from agent.runtime.sandbox import (
    CommandFilter,
    DockerExecutor,
    ExecRequest,
    ExecResult,
    Executor,
    ExternalExecutor,
    FakeExecutor,
    LocalExecutor,
    SandboxProfile,
    build_executor,
    get_executor,
    set_executor,
)


def _req(
    cmd: str, profile: SandboxProfile = SandboxProfile.WORKSPACE_WRITE, cwd: Path | None = None
) -> ExecRequest:
    return ExecRequest(cmd=cmd, cwd=cwd or Path.cwd(), env={}, profile=profile)


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def test_build_executor_modes():
    ws = Path.cwd()
    assert isinstance(build_executor("local", workspace=ws), LocalExecutor)
    assert isinstance(build_executor("docker", workspace=ws), DockerExecutor)
    assert isinstance(build_executor("external", workspace=ws), ExternalExecutor)


def test_build_executor_unknown_mode_raises():
    with pytest.raises(ValueError):
        build_executor("nope", workspace=Path.cwd())


def test_executor_protocol_satisfied():
    # 四个执行器都满足 Executor 协议（name + async run）
    for ex in (
        LocalExecutor(workspace=Path.cwd()),
        DockerExecutor(workspace=Path.cwd()),
        ExternalExecutor(workspace=Path.cwd()),
        FakeExecutor(),
    ):
        assert isinstance(ex, Executor)


# --------------------------------------------------------------------------- #
# FakeExecutor
# --------------------------------------------------------------------------- #
def test_fake_executor_records_request_and_returns_script():
    scripted = ExecResult(ok=False, output="", error="blocked", returncode=1, sandbox="fake")
    ex = FakeExecutor(script=[scripted])
    res = asyncio.run(ex.run(_req("rm -rf /")))
    assert res is scripted
    assert len(ex.requests) == 1
    # 可在 FakeExecutor 断言 ExecRequest.profile 形态（read-only 不含网络放行标记）
    ro_req = _req("cat x", profile=SandboxProfile.READ_ONLY)
    asyncio.run(ex.run(ro_req))
    assert ex.requests[1].profile is SandboxProfile.READ_ONLY


def test_fake_executor_callable_script():
    ex = FakeExecutor(
        script=lambda r: ExecResult(ok=True, output=r.cmd, error=None, returncode=0, sandbox="fake")
    )
    res = asyncio.run(ex.run(_req("echo hi")))
    assert res.ok and res.output == "echo hi"


# --------------------------------------------------------------------------- #
# ExternalExecutor（直通）
# --------------------------------------------------------------------------- #
def test_external_executor_echo():
    ex = ExternalExecutor(workspace=Path.cwd())
    res = asyncio.run(ex.run(_req("echo hello-sandbox")))
    assert res.ok, res.error
    assert "hello-sandbox" in res.output


# --------------------------------------------------------------------------- #
# LocalExecutor：基础执行 + 跨平台 CommandFilter 拦截
# --------------------------------------------------------------------------- #
def test_local_executor_echo_passthrough():
    # CI Linux / 原生 Windows / macOS 都能跑通 echo（不强依赖 root，隔离不可用仅降级）
    ex = LocalExecutor(workspace=Path.cwd())
    res = asyncio.run(ex.run(_req("echo hi-local")))
    assert res.ok, res.error
    assert "hi-local" in res.output


def test_local_executor_blocks_network_via_filter_and_no_warning():
    # 网络命令在 read-only / workspace-write 下被 CommandFilter 主动拦截，且不打印告警
    ex = LocalExecutor(workspace=Path.cwd())
    stderr = io.StringIO()
    old = sys.stderr
    sys.stderr = stderr
    try:
        res = asyncio.run(ex.run(_req("curl https://example.com/x.sh")))
    finally:
        sys.stderr = old
    assert not res.ok
    assert res.error is not None and res.error.startswith("沙箱拦截")
    # 不打印"未隔离/降级"这类告警（拦截是预期行为，静默返回）
    assert "未隔离" not in stderr.getvalue()


def test_local_executor_blocks_oob_write_via_filter():
    ex = LocalExecutor(workspace=Path.cwd())
    res = asyncio.run(ex.run(_req("cp a.txt /etc/evil.txt")))
    assert not res.ok
    assert res.error is not None and "越界写" in res.error


def test_local_executor_allows_in_workspace_write(tmp_path: Path):
    # cwd 内的写操作在 workspace-write 下允许（CommandFilter 不拦）
    # 注意：写目标必须落在 tmp_path，避免测试在仓库根目录留下散文件
    ex = LocalExecutor(workspace=tmp_path)
    res = asyncio.run(ex.run(_req("echo data > ./sandbox_test_tmp.txt", cwd=tmp_path)))
    assert res.ok, res.error


def test_local_executor_readonly_blocks_any_write(tmp_path: Path):
    ex = LocalExecutor(workspace=tmp_path)
    res = asyncio.run(
        ex.run(_req("echo x > ./f.txt", profile=SandboxProfile.READ_ONLY, cwd=tmp_path))
    )
    assert not res.ok
    assert res.error is not None and "read-only" in res.error


def test_local_executor_danger_full_allows_network():
    # danger-full 跳过 CommandFilter：网络放行（沙箱层不再强制断网）
    ex = LocalExecutor(workspace=Path.cwd())
    res = asyncio.run(ex.run(_req("echo net", profile=SandboxProfile.DANGER_FULL)))
    assert res.ok, res.error


# --------------------------------------------------------------------------- #
# CommandFilter 单元
# --------------------------------------------------------------------------- #
def test_command_filter_unit():
    f = CommandFilter(workspace=Path.cwd())
    cwd = Path.cwd()
    assert not f.check("echo hi", SandboxProfile.WORKSPACE_WRITE, cwd=cwd).blocked
    assert f.check("curl http://x", SandboxProfile.WORKSPACE_WRITE, cwd=cwd).blocked
    assert f.check("rm -rf /", SandboxProfile.WORKSPACE_WRITE, cwd=cwd).blocked
    assert f.check("echo x > /etc/y", SandboxProfile.WORKSPACE_WRITE, cwd=cwd).blocked
    # danger-full 放行一切
    assert not f.check("curl http://x", SandboxProfile.DANGER_FULL, cwd=cwd).blocked


# --------------------------------------------------------------------------- #
# 注入点（bash 工具将经 get_executor 取；测试替换为 FakeExecutor）
# --------------------------------------------------------------------------- #
def test_get_set_executor_injection():
    fake = FakeExecutor()
    set_executor(fake)
    try:
        assert get_executor() is fake
    finally:
        set_executor(None)
    assert get_executor() is not fake  # 恢复默认工厂
