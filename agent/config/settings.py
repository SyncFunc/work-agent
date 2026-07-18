"""分层配置（provider 无关）。

优先级（高 → 低）：
    CLI 参数 (init) > 环境变量 / .env > 项目级 YAML > 用户级 YAML > 内置默认

配置文件位置（与 CODEBUDDY.md 的隔离约定一致）：
    - 项目级：<project>/.agent/settings.yaml   （随项目，可被 gitignore，优先级高）
    - 用户级：~/.agent/settings.yaml            （跨项目个人偏好，优先级低）
    二者路径可被环境变量 AGENT_PROJECT_ROOT / AGENT_USER_CONFIG_DIR 覆盖（便于测试与容器）。

字段统一以 `llm_` 前缀对应环境变量 `LLM_*`（如 LLM_API_KEY）。
YAML 支持扁平键（llm_model: ...）或嵌套块（llm: { model: ... }），加载时自动展平。
注意：密钥（llm_api_key）建议放环境变量 / .env，不要写进会提交的 YAML。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# 内置默认（最低优先级，仅作兜底）
_BUILTIN_DEFAULTS: dict[str, Any] = {
    "llm_base_url": "https://api.deepseek.com",
    "llm_model": "deepseek-v4-flash",
    "max_iterations": 25,
    "max_tool_concurrency": 5,
    "max_repeat_calls": 3,
    "max_tool_output_chars": 20000,
}


def user_config_path() -> Path:
    """用户级配置：~/.agent/settings.yaml（可被 AGENT_USER_CONFIG_DIR 覆盖）。"""
    base = os.environ.get("AGENT_USER_CONFIG_DIR")
    root = Path(base) if base else Path.home() / ".agent"
    return root / "settings.yaml"


def project_config_path() -> Path:
    """项目级配置：<AGENT_PROJECT_ROOT 或 cwd>/.agent/settings.yaml。"""
    root = os.environ.get("AGENT_PROJECT_ROOT")
    base = Path(root) if root else Path.cwd()
    return base / ".agent" / "settings.yaml"


def _flatten(cfg: dict[str, Any]) -> dict[str, Any]:
    """展平一层嵌套：{llm: {model: x}} -> {llm_model: x}。其余键原样保留。"""
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        if isinstance(v, dict):
            for sub_k, sub_v in v.items():
                out[f"{k}_{sub_k}"] = sub_v
        else:
            out[k] = v
    return out


class YamlConfigSource(PydanticBaseSettingsSource):
    """合并 用户级 + 项目级 YAML；项目级覆盖用户级。作为 pydantic-settings 的一个源。

    pydantic-settings 的字段填充主循环调用 `get_field_value`（而非 `__call__`），
    故在 __init__ 预计算合并字典，并由 get_field_value 逐字段返回。
    """

    def __init__(self, settings_cls):
        super().__init__(settings_cls)
        self._data = self._load_merged()

    def _load_merged(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        valid = set(self.settings_cls.model_fields.keys())

        # 用户级（低优先级）先载入
        up = user_config_path()
        if up.is_file():
            data.update(_flatten(_load_yaml(up)))

        # 项目级（高优先级）覆盖用户级
        pp = project_config_path()
        if pp.is_file():
            data.update(_flatten(_load_yaml(pp)))

        # 仅保留模型中真实存在的字段
        return {k: v for k, v in data.items() if k in valid}

    def get_field_value(self, field, field_name: str) -> tuple[Any, str, bool]:
        if field_name in self._data:
            return self._data[field_name], field_name, False
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


def _load_yaml(p: Path) -> dict[str, Any]:
    try:
        with p.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _find_dotenv(start: Path | None = None) -> str | None:
    """从 start（默认 cwd）向上查找 .env，最多上溯 10 层。

    这样无论从项目哪个子目录运行（如 `python -m agent.cli chat` 在 agent/ 下执行），
    都能加载到项目根目录的 .env，而不是只依赖「cwd 恰好等于项目根」。
    也会直接检查 AGENT_PROJECT_ROOT 指向目录下的 .env。找不到返回 None。
    """
    cur = (start or Path.cwd()).resolve()
    for _ in range(10):
        cand = cur / ".env"
        if cand.is_file():
            return str(cand)
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    root = os.environ.get("AGENT_PROJECT_ROOT")
    if root:
        p = Path(root) / ".env"
        if p.is_file():
            return str(p)
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM（OpenAI 兼容）
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-v4-flash"

    # 命令执行（bash 工具）：shell 可执行文件覆盖（如 "bash" / "D:/Program Files/Git/bin/bash.exe"）。
    # 为空 = 自动：Windows 优先 git-bash（支持 linux 命令且输出 UTF-8），回退 cmd.exe；
    # 非 Windows 用 /bin/sh。值会被按空格拆分后作为前缀，命令以 `-c` 传入。
    bash_shell: str | None = None

    # 循环控制
    max_iterations: int = 25
    max_tool_concurrency: int = 5   # 同轮 tool_calls 并发上限（Semaphore）
    max_repeat_calls: int = 3       # 相邻轮相同调用签名重复达此值 → 判卡死
    max_tool_output_chars: int = 20000  # 工具输出超长截断（保护上下文）

    # 意图澄清（M1.5）：模糊任务先 ask_clarification 再执行
    clarify_enabled: bool = True
    max_clarify_rounds: int = 2      # 跨 run 累计允许的最多澄清轮次（防死循环护栏）
    clarify_hint_min_chars: int = 0  # ≤0 关闭；>0 时任务短于此值才在 system 提示强化澄清倾向

    # PLAN 模式（M1.4）：只读探索 → 计划落盘 → 确认后执行（update_plan 回写进度）
    plan_mode: bool = False                       # 默认关；CLI `--plan` 开启
    plan_mode_block_risk_above: str = "read"      # plan 模式下允许的最高风险；高于则拦截（默认只放行 read）
    plan_file: str = ".agent/plan.md"             # 计划文件落盘路径（相对 cwd，M5 可改 session 级）
    # PLAN 模式下 bash 工具的「只读命令白名单」：仅这些命令放行（满足探索需求），
    # 其余 exec 仍被风险门控拦截。默认值覆盖常见只读探索命令；支持多词前缀（如 "git status"）。
    # 可在项目/用户 settings.yaml 用 plan_mode_bash_allow 覆盖或追加。
    plan_mode_bash_allow: list[str] = [
        "ls", "ll", "dir", "find", "cat", "head", "tail", "grep", "rg", "pwd",
        "echo", "wc", "tree", "which", "where", "file", "stat", "readlink",
        "uname", "date", "env", "printenv", "ps", "type", "command -v",
        "git status", "git log", "git diff", "git branch", "git show",
        "git remote", "git stash list", "git tag", "git config --get",
    ]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # 优先级（高 → 低）：CLI(init) > env > .env > YAML(项目>用户) > 内置默认
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSource(settings_cls),
            file_secret_settings,
        )


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
    """构建 Settings。

    - project_root：指定项目根（决定项目级 YAML 位置），仅用于测试/嵌入场景；
      为空时回退到环境变量 AGENT_PROJECT_ROOT 或当前工作目录。
    - overrides：CLI 参数覆盖（如 model="x"），优先级最高。
    """
    overrides = {k: v for k, v in overrides.items() if v is not None}
    with _env_override(AGENT_PROJECT_ROOT=project_root):
        kwargs: dict[str, Any] = {}
        # 让 .env 基于项目根解析（向上查找），而非仅 cwd（见 _find_dotenv）
        env_file = _find_dotenv()
        if env_file:
            kwargs["_env_file"] = env_file
        return Settings(**kwargs, **overrides)
