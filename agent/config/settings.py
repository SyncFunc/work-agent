"""分层配置，嵌套类结构，YAML 层级直接映射。

配置来源（优先级高 → 低）：
    CLI 参数 (init) > 项目级 YAML > 用户级 YAML > 内置默认

配置文件位置：
    - 项目级：<project>/.agent/settings.yaml   （随项目，可被 gitignore，优先级高）
    - 用户级：~/.agent/settings.yaml            （跨项目个人偏好，优先级低）
    路径可被环境变量 AGENT_PROJECT_ROOT / AGENT_USER_CONFIG_DIR 覆盖（便于测试与容器）。

访问方式（点号路径）：
    settings.llm.model           # LLM 模型名
    settings.loop.max_iterations # 循环上限
    settings.sandbox.profile     # 沙箱档位
    settings.approval.mode       # 审批模式
    settings.plan.mode           # PLAN 模式开关
    settings.clarify.enabled     # 意图澄清开关

密钥（llm.api_key）建议通过 CLI 参数或 YAML 配置，不写进版本控制。
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

import yaml
from pydantic import BaseModel
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


# --------------------------------------------------------------------------- #
# 嵌套子模型（与 YAML 层级一一对应）
# --------------------------------------------------------------------------- #
class LLMConfig(BaseModel):
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"


class LoopConfig(BaseModel):
    max_iterations: int = 25
    max_tool_concurrency: int = 5
    max_repeat_calls: int = 3
    max_tool_output_chars: int = 20000


class SandboxConfig(BaseModel):
    mode: str = "local"
    profile: str = "workspace-write"


class ApprovalConfig(BaseModel):
    mode: str = "on-request"
    exec_policy: list[str] = []
    noninteractive_default: str = "allow"
    elevated_sandbox_profile: str = "danger-full"


class PlanConfig(BaseModel):
    mode: bool = False
    file: str = ".agent/plan.md"


class ClarifyConfig(BaseModel):
    enabled: bool = True
    max_rounds: int = 2
    hint_min_chars: int = 0


class BashConfig(BaseModel):
    shell: str | None = None


class ObsConfig(BaseModel):
    """可观测配置（M3.1）。"""

    enabled: bool = True
    db_path: str = ".agent/traces.db"


class RateLimitConfigModel(BaseModel):
    llm_max_calls: int = 60
    llm_window_seconds: int = 60
    sandbox_max_calls: int = 120
    sandbox_window_seconds: int = 60


class CircuitBreakerConfigModel(BaseModel):
    llm_failure_threshold: int = 5
    llm_recovery_timeout: float = 30.0
    sandbox_failure_threshold: int = 10
    sandbox_recovery_timeout: float = 60.0


class FallbackConfigModel(BaseModel):
    llm_strategy: str = "retry"
    sandbox_strategy: str = "fail_fast"


class ResilienceConfig(BaseModel):
    """韧性层配置（M3.2）。"""

    enabled: bool = True
    rate_limit: RateLimitConfigModel = RateLimitConfigModel()
    circuit_breaker: CircuitBreakerConfigModel = CircuitBreakerConfigModel()
    fallback: FallbackConfigModel = FallbackConfigModel()


class ContextConfig(BaseModel):
    """上下文与记忆配置（M4 上下文与记忆里程碑）。"""

    context_window: int = 200_000
    max_output_tokens: int = 20_000
    compact_buffer: int = 13_000
    microcompact_keep_recent: int = 5
    microcompact_enabled: bool = True
    auto_compact_enabled: bool = True
    session_memory_enabled: bool = True
    session_memory_dir: str = ".agent/sessions"
    agents_md_path: str = "AGENTS.md"
    agents_md_enabled: bool = True


class SkillsConfig(BaseModel):
    """Skill 能力开关（M5.1/M5.3）。"""

    enabled: bool = True
    dirs: list[str] = []  # 额外 skill 目录（除项目级 .agent/skills、用户级 ~/.agent/skills）


class SubagentsConfig(BaseModel):
    """子 Agent 能力开关（M5.2/M5.3）。"""

    enabled: bool = True
    max_depth: int = 5
    auto_allow: bool = False


# --------------------------------------------------------------------------- #
# 主 Settings
# --------------------------------------------------------------------------- #
class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    llm: LLMConfig = LLMConfig()
    loop: LoopConfig = LoopConfig()
    sandbox: SandboxConfig = SandboxConfig()
    approval: ApprovalConfig = ApprovalConfig()
    plan: PlanConfig = PlanConfig()
    clarify: ClarifyConfig = ClarifyConfig()
    bash: BashConfig = BashConfig()
    obs: ObsConfig = ObsConfig()
    resilience: ResilienceConfig = ResilienceConfig()
    context: ContextConfig = ContextConfig()
    skills: SkillsConfig = SkillsConfig()
    subagents: SubagentsConfig = SubagentsConfig()




    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (
            init_settings,
            YamlConfigSource(settings_cls),
        )


# --------------------------------------------------------------------------- #
# YAML 加载
# --------------------------------------------------------------------------- #
def user_config_path() -> Path:
    base = os.environ.get("AGENT_USER_CONFIG_DIR")
    root = Path(base) if base else Path.home() / ".agent"
    return root / "settings.yaml"


def project_config_path() -> Path:
    root = os.environ.get("AGENT_PROJECT_ROOT")
    base = Path(root) if root else Path.cwd()
    return base / ".agent" / "settings.yaml"


def _load_yaml(p: Path) -> dict[str, Any]:
    try:
        with p.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


class YamlConfigSource(PydanticBaseSettingsSource):
    """合并 用户级 + 项目级 YAML；项目级覆盖用户级。"""

    def __init__(self, settings_cls):
        super().__init__(settings_cls)
        self._data = self._load_merged()

    def _load_merged(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        up = user_config_path()
        if up.is_file():
            data.update(_load_yaml(up))
        pp = project_config_path()
        if pp.is_file():
            data.update(_load_yaml(pp))
        return data

    def get_field_value(self, field, field_name: str) -> tuple[Any, str, bool]:
        if field_name in self._data:
            return self._data[field_name], field_name, False
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


# --------------------------------------------------------------------------- #
# load_settings 快捷入口
# --------------------------------------------------------------------------- #
@contextmanager
def _env_override(**kv: Any):
    prev: dict[str, str | None] = {}
    for k, v in kv.items():
        if v is None:
            continue
        prev[k] = os.environ.get(k)
        os.environ[k] = str(v)
    try:
        yield
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def load_settings(project_root: str | Path | None = None, **overrides: Any) -> Settings:
    overrides = {k: v for k, v in overrides.items() if v is not None}
    with _env_override(AGENT_PROJECT_ROOT=project_root):
        return Settings(**overrides)
