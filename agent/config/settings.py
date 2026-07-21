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
    # M4.4 Session Memory 增量更新阈值（token 增量为必要条件）
    session_memory_min_message_tokens: int = 10_000   # 初次触发所需上下文 token 数
    session_memory_min_tokens_between: int = 5_000     # 两次更新最小 token 增量
    session_memory_tool_calls_between: int = 3         # 两次更新最少 tool call 次数
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


class DaemonConfig(BaseModel):
    """守护进程（M7 agentrunner 分离）配置。

    仅绑定回环地址；端口/健康检查端口可配；可选本机 token 鉴权。
    """

    host: str = "127.0.0.1"
    port: int = 18789
    health_port: int = 18790
    token: str = ""  # 非空时要求客户端 hello 携带相同 token（本机鉴权，可选）


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
    daemon: DaemonConfig = DaemonConfig()




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


# --------------------------------------------------------------------------- #
# 首次运行脚手架（M4.5 增强）：在项目级 .agent/ 下生成配置骨架
# --------------------------------------------------------------------------- #
_AGENTS_MD_TEMPLATE = """# AGENTS.md（项目约定）

本文件由 Agent 自动维护，记录项目级约定与上下文。
- 在此填写项目的编码规范、目录结构、常用命令等。
- 模型每次请求都会读取本文件（永不压缩）。
- 也可在项目根创建 AGENTS.md 并提交到版本库，优先级更高。
"""


def scaffold_project(
    project_root: str | Path | None = None,
    *,
    create_if_exists: bool = False,
) -> dict[str, bool]:
    """首次运行 scaffolding：在项目级 ``.agent/`` 下生成配置骨架。

    产物：
      - ``.agent/settings.yaml``（若不存在，从 ``settings.example.yaml`` 复制）
      - ``.agent/skills/``、``.agent/agents/``（空目录占位）
      - ``.agent/AGENTS.md``（最小模板，若缺失）

    ``create_if_exists=False``（自动首次运行）：若 ``.agent/`` 已存在则**整体跳过**，
    避免在既有项目中误建目录/文件（幂等、不污染）。
    ``create_if_exists=True``（显式 ``init`` 命令）：即便 ``.agent/`` 已存在，也补齐
    缺失项，但**绝不覆盖已存在的 ``settings.yaml``**（避免破坏用户配置）。

    返回各产物是否新建的字典（空字典表示整体跳过）。
    """
    root = Path(project_root) if project_root else Path(
        os.environ.get("AGENT_PROJECT_ROOT") or Path.cwd()
    )
    agent_dir = root / ".agent"

    # 自动模式：已有 .agent 则不再触碰
    if agent_dir.exists() and not create_if_exists:
        return {}

    agent_dir.mkdir(parents=True, exist_ok=True)
    created: dict[str, bool] = {}

    # settings.yaml（从示例复制；已存在则不覆盖）
    settings_path = agent_dir / "settings.yaml"
    if not settings_path.exists():
        example = Path(__file__).parent / "settings.example.yaml"
        text = example.read_text(encoding="utf-8") if example.exists() else ""
        settings_path.write_text(text, encoding="utf-8")
        created["settings.yaml"] = True
    else:
        created["settings.yaml"] = False

    # skills / agents 目录占位
    for sub in ("skills", "agents"):
        d = agent_dir / sub
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created[sub] = True
        else:
            created[sub] = False

    # AGENTS.md（自动维护版；已存在则不覆盖）
    agents_md = agent_dir / "AGENTS.md"
    if not agents_md.exists():
        agents_md.write_text(_AGENTS_MD_TEMPLATE, encoding="utf-8")
        created["AGENTS.md"] = True
    else:
        created["AGENTS.md"] = False

    return created
