"""配置分层验收：内置默认 → 用户级 YAML → 项目级 YAML → CLI。

Settings 使用嵌套类结构（LLMConfig / LoopConfig / SandboxConfig 等），
YAML 直接映射到嵌套字段。不支持环境变量 / .env。
"""

from pathlib import Path

from agent.config.settings import Settings, load_settings


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _isolate(monkeypatch, tmp_path: Path):
    """把用户/项目配置都指到临时空目录。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_USER_CONFIG_DIR", str(tmp_path / "user"))
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(tmp_path / "proj"))


def test_defaults_without_any_config(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    s = Settings()
    assert s.llm.model == "deepseek-v4-flash"
    assert s.llm.base_url == "https://api.deepseek.com"
    assert s.loop.max_iterations == 25


def test_user_yaml_overrides_defaults(monkeypatch, tmp_path):
    user_dir = tmp_path / "user"
    _write(user_dir / "settings.yaml", "llm:\n  model: user-model\nloop:\n  max_iterations: 10\n")
    _isolate(monkeypatch, tmp_path)
    s = Settings()
    assert s.llm.model == "user-model"
    assert s.loop.max_iterations == 10


def test_project_yaml_overrides_user_yaml(monkeypatch, tmp_path):
    user_dir = tmp_path / "user"
    proj_dir = tmp_path / "proj"
    _write(user_dir / "settings.yaml", "llm:\n  model: user-model\nloop:\n  max_iterations: 10\n")
    _write(proj_dir / ".agent" / "settings.yaml", "llm:\n  model: proj-model\n")
    _isolate(monkeypatch, tmp_path)
    s = Settings()
    assert s.llm.model == "proj-model"
    assert s.loop.max_iterations == 10


def test_load_settings_project_root_arg(monkeypatch, tmp_path):
    proj_dir = tmp_path / "proj"
    _write(proj_dir / ".agent" / "settings.yaml", "llm:\n  model: arg-model\n")
    _isolate(monkeypatch, tmp_path)
    s = load_settings(project_root=proj_dir)
    assert s.llm.model == "arg-model"


def test_cli_override_is_highest(monkeypatch, tmp_path):
    proj_dir = tmp_path / "proj"
    _write(proj_dir / ".agent" / "settings.yaml", "llm:\n  model: proj-model\n")
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PROJECT_ROOT", str(proj_dir))
    s = load_settings(llm=dict(model="cli-model"))
    assert s.llm.model == "cli-model"


# --------------------------------------------------------------------------- #
# M2.3 分层权限配置：沙箱与审批字段默认值
# --------------------------------------------------------------------------- #
def test_m23_defaults_without_any_config(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    s = Settings()
    assert s.sandbox.mode == "local"
    assert s.sandbox.profile == "workspace-write"
    assert s.approval.mode == "on-request"
    assert s.approval.exec_policy == []
    assert s.approval.noninteractive_default == "allow"
    assert s.approval.elevated_sandbox_profile == "danger-full"


def test_m23_yaml_overrides(monkeypatch, tmp_path):
    user_dir = tmp_path / "user"
    _write(
        user_dir / "settings.yaml",
        "sandbox:\n  mode: docker\n  profile: read-only\n"
        "approval:\n  mode: unless-trusted\n"
        "  exec_policy:\n    - ls\n    - cat\n  noninteractive_default: deny\n",
    )
    _isolate(monkeypatch, tmp_path)
    s = Settings()
    assert s.sandbox.mode == "docker"
    assert s.sandbox.profile == "read-only"
    assert s.approval.mode == "unless-trusted"
    assert s.approval.exec_policy == ["ls", "cat"]
    assert s.approval.noninteractive_default == "deny"
    assert s.approval.elevated_sandbox_profile == "danger-full"


def test_m23_approval_exec_policy_default_empty_list(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    s = Settings()
    assert s.approval.exec_policy == []
    assert isinstance(s.approval.exec_policy, list)
