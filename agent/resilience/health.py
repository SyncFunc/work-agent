"""健康检查核心（M3.4）：注册制健康检查 + 聚合状态 + CLI health 命令 + HTTP 端点。

设计：
- ``HealthChecker`` 是注册制，检查函数异步并发执行（``asyncio.gather``）。
- 结果分级：ok（正常）/ degraded（非致命问题）/ fail（关键链路中断）。
- HTTP 端点用标准库 ``http.server`` + ``run_in_executor``，避免引入 FastAPI。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from typing import Any


@dataclass
class CheckResult:
    name: str
    status: str = "ok"  # ok / degraded / fail
    detail: str = ""
    duration_ms: float = 0.0


@dataclass
class HealthStatus:
    healthy: bool
    checks: dict[str, CheckResult] = field(default_factory=dict)
    timestamp: float = 0.0


class HealthChecker:
    """注册制健康检查执行器。"""

    def __init__(self) -> None:
        self._checks: dict[str, Callable[[], Awaitable[CheckResult]]] = {}

    def register(self, name: str, check_fn: Callable[[], Awaitable[CheckResult]]) -> None:
        """注册一个健康检查函数。"""
        self._checks[name] = check_fn

    def registered(self) -> list[str]:
        """返回已注册的检查项名称列表。"""
        return list(self._checks.keys())

    async def check_all(self) -> HealthStatus:
        """并发执行全部已注册检查，返回聚合状态。"""
        results: list[CheckResult | BaseException] = await asyncio.gather(
            *[self._run_check(name, fn) for name, fn in self._checks.items()],
            return_exceptions=True,
        )
        checks: dict[str, CheckResult] = {}
        overall_healthy = True
        for r in results:
            if isinstance(r, BaseException):
                cr = CheckResult(name="unknown", status="fail", detail=str(r))
            else:
                cr = r
            checks[cr.name] = cr
            if cr.status == "fail":
                overall_healthy = False
            elif cr.status == "degraded":
                overall_healthy = False
        return HealthStatus(healthy=overall_healthy, checks=checks, timestamp=time.time())

    @staticmethod
    async def _run_check(
        name: str, fn: Callable[[], Awaitable[CheckResult]]
    ) -> CheckResult:
        start = time.time()
        try:
            result = await fn()
            result.duration_ms = (time.time() - start) * 1000
            return result
        except Exception as e:
            return CheckResult(
                name=name,
                status="fail",
                detail=str(e),
                duration_ms=(time.time() - start) * 1000,
            )


# --------------------------------------------------------------------------- #
# 默认健康检查项工厂
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# HTTP 健康端点 handler
# --------------------------------------------------------------------------- #
_HTTP_CHECKER: HealthChecker | None = None


class HealthHTTPHandler(BaseHTTPRequestHandler):
    """HTTP GET /health 端点 handler。checker 从模块级变量 ``_HTTP_CHECKER`` 获取。"""

    def do_GET(self) -> None:  # noqa: N802
        import asyncio

        if self.path == "/health":
            checker = _HTTP_CHECKER
            if checker is None:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"healthy":false,"error":"health checker not initialized"}')
                return
            status = asyncio.run(checker.check_all())
            body = json.dumps(
                {
                    "healthy": status.healthy,
                    "checks": {
                        k: {"status": v.status, "detail": v.detail}
                        for k, v in status.checks.items()
                    },
                    "timestamp": status.timestamp,
                },
                indent=2,
            )
            self.send_response(200 if status.healthy else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_response(404)
            self.end_headers()


def build_default_health_checks(settings: Any) -> HealthChecker:
    """构建包含默认检查项的 HealthChecker。

    检查项：llm（非致命）、sandbox（非致命）、sqlite（关键）、registry（关键）。
    """
    checker = HealthChecker()

    # registry 检查
    async def _check_registry() -> CheckResult:
        from agent.runtime.registry import default_registry

        tools = default_registry.list()
        if len(tools) >= 3:
            return CheckResult(
                name="registry", status="ok", detail=f"{len(tools)} tools registered"
            )
        return CheckResult(name="registry", status="degraded", detail=f"only {len(tools)} tools")

    # sqlite 检查
    async def _check_sqlite() -> CheckResult:
        try:
            from agent.obs.store import TraceStore

            store = TraceStore(settings.obs.db_path)
            store.list_sessions()
            return CheckResult(name="sqlite", status="ok", detail=f"db={settings.obs.db_path}")
        except Exception as e:
            return CheckResult(name="sqlite", status="fail", detail=str(e))

    # sandbox 检查
    async def _check_sandbox() -> CheckResult:
        from pathlib import Path

        from agent.runtime.sandbox import ExecRequest, SandboxProfile, build_executor

        try:
            ex = build_executor(
                settings.sandbox.mode,
                workspace=Path.cwd(),
                profile=SandboxProfile(settings.sandbox.profile),
            )
            req = ExecRequest(
                cmd="echo ok", cwd=Path.cwd(), env={}, timeout=5, profile=SandboxProfile.DANGER_FULL
            )
            result = await ex.run(req)
            if result.ok:
                return CheckResult(
                    name="sandbox", status="ok", detail=f"mode={settings.sandbox.mode}"
                )
            return CheckResult(
                name="sandbox", status="degraded", detail=result.error or "unknown error"
            )
        except Exception as e:
            return CheckResult(name="sandbox", status="degraded", detail=str(e))

    checker.register("registry", _check_registry)
    checker.register("sqlite", _check_sqlite)
    checker.register("sandbox", _check_sandbox)

    return checker
