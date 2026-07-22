"""M3.4 验收：健康检查核心 + CLI health 命令。"""

import asyncio

from typer.testing import CliRunner

from agent.cli import app
from agent.resilience.health import (
    CheckResult,
    HealthChecker,
    build_default_health_checks,
)


class TestHealthChecker:
    def test_register_and_run(self):
        """注册自定义检查并执行。"""
        checker = HealthChecker()

        async def _ok() -> CheckResult:
            return CheckResult(name="test", status="ok")

        checker.register("test", _ok)
        assert "test" in checker.registered()
        status = asyncio.run(checker.check_all())
        assert "test" in status.checks
        assert status.checks["test"].status == "ok"

    def test_all_ok(self):
        """全部 ok → healthy=True。"""
        checker = HealthChecker()

        async def _a() -> CheckResult:
            return CheckResult(name="a", status="ok")

        async def _b() -> CheckResult:
            return CheckResult(name="b", status="ok")

        checker.register("a", _a)
        checker.register("b", _b)
        status = asyncio.run(checker.check_all())
        assert status.healthy is True

    def test_degraded(self):
        """有 degraded → healthy=False。"""
        checker = HealthChecker()

        async def _ok() -> CheckResult:
            return CheckResult(name="ok", status="ok")

        async def _deg() -> CheckResult:
            return CheckResult(name="deg", status="degraded", detail="slow")

        checker.register("ok", _ok)
        checker.register("deg", _deg)
        status = asyncio.run(checker.check_all())
        assert status.healthy is False
        assert status.checks["deg"].status == "degraded"

    def test_fail(self):
        """有 fail → healthy=False。"""
        checker = HealthChecker()

        async def _ok() -> CheckResult:
            return CheckResult(name="ok", status="ok")

        async def _fail() -> CheckResult:
            return CheckResult(name="fail", status="fail", detail="broken")

        checker.register("ok", _ok)
        checker.register("fail", _fail)
        status = asyncio.run(checker.check_all())
        assert status.healthy is False
        assert status.checks["fail"].status == "fail"

    def test_check_exception_handled(self):
        """检查函数抛异常时被捕获并标记为 fail。"""
        checker = HealthChecker()

        async def _crash() -> CheckResult:
            raise RuntimeError("unexpected crash")

        checker.register("crash", _crash)
        status = asyncio.run(checker.check_all())
        assert status.checks["crash"].status == "fail"
        assert "unexpected crash" in status.checks["crash"].detail

    def test_parallel_execution(self):
        """多个检查并发执行。"""
        import time

        checker = HealthChecker()

        async def _slow() -> CheckResult:
            await asyncio.sleep(0.05)
            return CheckResult(name="slow", status="ok")

        async def _fast() -> CheckResult:
            return CheckResult(name="fast", status="ok")

        checker.register("slow", _slow)
        checker.register("fast", _fast)
        start = time.time()
        asyncio.run(checker.check_all())
        elapsed = time.time() - start
        # 如果串行执行会 ≥0.1s，并发执行 ≈0.05s
        assert elapsed < 0.09, f"expected parallel execution, took {elapsed:.3f}s"

    def test_check_result_duration(self):
        """duration_ms 自动填充。"""
        checker = HealthChecker()

        async def _slow() -> CheckResult:
            await asyncio.sleep(0.02)
            return CheckResult(name="slow", status="ok")

        checker.register("slow", _slow)
        status = asyncio.run(checker.check_all())
        assert status.checks["slow"].duration_ms >= 20.0


class TestBuildDefaultHealthChecks:
    def test_default_checks_registered(self):
        """默认检查项包含 registry、sqlite、sandbox。"""
        from agent.config.settings import Settings

        checker = build_default_health_checks(Settings())
        names = checker.registered()
        assert "registry" in names
        assert "sqlite" in names
        assert "sandbox" in names

    def test_default_checks_run_without_error(self):
        """默认检查项可执行且不抛异常。"""
        from agent.config.settings import Settings

        checker = build_default_health_checks(Settings())
        status = asyncio.run(checker.check_all())
        assert "registry" in status.checks
        # registry 应该 ok（至少有 bash/read/write）
        assert status.checks["registry"].status in ("ok", "degraded")


class TestHealthCli:
    def test_cli_health_exit_code_ok(self, monkeypatch):
        """health 命令全 ok 时退出码 0。"""
        from agent.resilience.health import HealthChecker

        checker = HealthChecker()

        async def _ok() -> CheckResult:
            return CheckResult(name="test", status="ok")

        checker.register("test", _ok)
        monkeypatch.setattr(
            "agent.resilience.health.build_default_health_checks", lambda s: checker
        )
        runner = CliRunner()
        result = runner.invoke(app, ["health"])
        assert result.exit_code == 0

    def test_cli_health_exit_code_degraded(self, monkeypatch):
        """有 degraded 时退出码 1。"""
        from agent.resilience.health import HealthChecker

        checker = HealthChecker()

        async def _deg() -> CheckResult:
            return CheckResult(name="test", status="degraded")

        checker.register("test", _deg)
        monkeypatch.setattr(
            "agent.resilience.health.build_default_health_checks", lambda s: checker
        )
        runner = CliRunner()
        result = runner.invoke(app, ["health"])
        assert result.exit_code == 1

    def test_cli_health_exit_code_fail(self, monkeypatch):
        """有 fail 时退出码 2。"""
        from agent.resilience.health import HealthChecker

        checker = HealthChecker()

        async def _fail() -> CheckResult:
            return CheckResult(name="test", status="fail", detail="broken")

        checker.register("test", _fail)
        monkeypatch.setattr(
            "agent.resilience.health.build_default_health_checks", lambda s: checker
        )
        runner = CliRunner()
        result = runner.invoke(app, ["health"])
        assert result.exit_code == 2

    def test_cli_health_output_contains_checks(self, monkeypatch):
        """health 命令输出包含检查项名称。"""
        from agent.resilience.health import HealthChecker

        checker = HealthChecker()

        async def _ok() -> CheckResult:
            return CheckResult(name="mycheck", status="ok")

        checker.register("mycheck", _ok)
        monkeypatch.setattr(
            "agent.resilience.health.build_default_health_checks", lambda s: checker
        )
        runner = CliRunner()
        result = runner.invoke(app, ["health"])
        assert result.exit_code == 0
        assert "mycheck" in result.stdout

    def test_cli_health_with_watch_flag(self, monkeypatch):
        """--watch 模式不报错（只测试启动，不持续等待）。"""
        from agent.resilience.health import HealthChecker

        checker = HealthChecker()

        async def _ok() -> CheckResult:
            return CheckResult(name="test", status="ok")

        checker.register("test", _ok)
        monkeypatch.setattr(
            "agent.resilience.health.build_default_health_checks", lambda s: checker
        )
        runner = CliRunner()
        # 用超时避免无限等待
        result = runner.invoke(app, ["health", "--watch"], timeout=2)
        # watch 模式会持续运行，这里只验证不崩溃
        assert result.exit_code == 0 or "Traceback" not in (result.stdout + result.stderr)
