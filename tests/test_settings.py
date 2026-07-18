"""M1.1 配置分层验收（精炼）：内置默认 → 用户级 YAML → 项目级 YAML → env/.env → CLI。

仅依赖临时目录与 monkeypatch，不碰真实 ~/.agent 或项目根。
"""

from pathlib import Path

import pytest

from agent.config.settings import Settings, load_settings


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _isolate(monkeypatch, tmp_path: Path):
    """把用户/项目配置都指到临时空目录，并清掉可能泄露的 .env 与 LLM_* 环境变量。

    注意：pydantic-settings 的 env_file='.env' 按 cwd 解析，故 chdir 到临时目录，
    避免读到项目根的真实 .env（env/.env 优先级高于 YAML，会干扰 YAML 覆盖测试）。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_USER_CONFIG_DIR", str(tmp_path / "user"))
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path / "proj"))
    for k in ("LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def test_defaults_without_any_config(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    s = Settings()
    assert s.llm_model == "deepseek-v4-flash"
    assert s.llm_base_url == "https://api.deepseek.com"
    assert s.max_iterations == 25


def test_user_yaml_overrides_defaults(monkeypatch, tmp_path):
    user_dir = tmp_path / "user"
    _write(user_dir / "settings.yaml", "llm:\n  model: user-model\nmax_iterations: 10\n")
    _isolate(monkeypatch, tmp_path)
    s = Settings()
    assert s.llm_model == "user-model"
    assert s.max_iterations == 10


def test_project_yaml_overrides_user_yaml(monkeypatch, tmp_path):
    user_dir = tmp_path / "user"
    proj_dir = tmp_path / "proj"
    _write(user_dir / "settings.yaml", "llm:\n  model: user-model\nmax_iterations: 10\n")
    _write(proj_dir / ".agent" / "settings.yaml", "llm:\n  model: proj-model\n")
    _isolate(monkeypatch, tmp_path)
    s = Settings()
    assert s.llm_model == "proj-model"      # 项目级覆盖用户级
    assert s.max_iterations == 10            # 项目级未设，沿用用户级


def test_env_overrides_project_yaml(monkeypatch, tmp_path):
    proj_dir = tmp_path / "proj"
    _write(proj_dir / ".agent" / "settings.yaml", "llm:\n  model: proj-model\n")
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_MODEL", "env-model")
    s = Settings()
    assert s.llm_model == "env-model"


def test_load_settings_project_root_arg(monkeypatch, tmp_path):
    proj_dir = tmp_path / "proj"
    _write(proj_dir / ".agent" / "settings.yaml", "llm:\n  model: arg-model\n")
    _isolate(monkeypatch, tmp_path)
    s = load_settings(project_root=proj_dir)
    assert s.llm_model == "arg-model"


def test_cli_override_is_highest(monkeypatch, tmp_path):
    proj_dir = tmp_path / "proj"
    _write(proj_dir / ".agent" / "settings.yaml", "llm:\n  model: proj-model\n")
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(proj_dir))
    s = load_settings(llm_model="cli-model")
    assert s.llm_model == "cli-model"


def test_flat_keys_also_supported(monkeypatch, tmp_path):
    user_dir = tmp_path / "user"
    _write(user_dir / "settings.yaml", "llm_model: flat-model\n")
    _isolate(monkeypatch, tmp_path)
    s = Settings()
    assert s.llm_model == "flat-model"
