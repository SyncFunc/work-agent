"""M5.1 SkillLoader 测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.skills.loader import SkillLoader
from agent.skills.spec import SkillSpec


# --------------------------------------------------------------------------- #
# 测试辅助
# --------------------------------------------------------------------------- #
def _write_skill(root: Path, name: str, frontmatter: str, body: str = "正文内容") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"{frontmatter}\n{body}\n", encoding="utf-8")
    return d


def _make_loader(tmp_path: Path, *, with_project: bool = True, with_user: bool = False):
    if with_project:
        proj = tmp_path / "proj"
        proj.mkdir(exist_ok=True)
    else:
        proj = tmp_path / "proj"
    # 始终用隔离的临时 user 根目录，避免扫描真实 ~/.agent/skills 造成环境依赖。
    user = tmp_path / "user"
    user.mkdir(exist_ok=True)
    return SkillLoader(proj, user_root=user)


# --------------------------------------------------------------------------- #
# 发现
# --------------------------------------------------------------------------- #
def test_discover_finds_project_and_user(tmp_path: Path):
    _write_skill(tmp_path / "proj" / ".agent" / "skills", "a", "---\nname: a\ndescription: A\n---")
    _write_skill(tmp_path / "user" / "skills", "b", "---\nname: b\ndescription: B\n---")
    loader = _make_loader(tmp_path, with_user=True)
    names = {s.name for s in loader.discover()}
    assert names == {"a", "b"}


def test_discover_project_overrides_user(tmp_path: Path):
    _write_skill(tmp_path / "user" / "skills", "shared",
                 "---\nname: shared\ndescription: 用户级\n---", body="用户正文")
    _write_skill(tmp_path / "proj" / ".agent" / "skills", "shared",
                 "---\nname: shared\ndescription: 项目级\n---", body="项目正文")
    loader = _make_loader(tmp_path, with_user=True)
    specs = loader.discover()
    assert len(specs) == 1
    shared = specs[0]
    assert shared.description == "项目级"
    assert shared.body() == "项目正文"


def test_discover_ignores_dir_without_skill_md(tmp_path: Path):
    d = tmp_path / "proj" / ".agent" / "skills" / "broken"
    d.mkdir(parents=True)
    (d / "README.md").write_text("no skill", encoding="utf-8")
    _write_skill(tmp_path / "proj" / ".agent" / "skills", "ok",
                 "---\nname: ok\ndescription: OK\n---")
    loader = _make_loader(tmp_path)
    names = {s.name for s in loader.discover()}
    assert names == {"ok"}


def test_get_returns_none_for_missing(tmp_path: Path):
    loader = _make_loader(tmp_path)
    assert loader.get("nope") is None


# --------------------------------------------------------------------------- #
# 解析 frontmatter
# --------------------------------------------------------------------------- #
def test_spec_parses_all_fields(tmp_path: Path):
    fm = (
        "---\n"
        "name: demo\n"
        "description: 演示\n"
        "when_to_use: 当需要时\n"
        "arguments: [file, lang]\n"
        "argument-hint: <file> <lang>\n"
        "disable-model-invocation: true\n"
        "user-invocable: false\n"
        "allowed-tools: [read, grep]\n"
        "disallowed-tools: [write]\n"
        "model: deepseek-chat\n"
        "effort: high\n"
        "context: fork\n"
        "agent: explore\n"
        "paths: ['*.py', 'src/**']\n"
        "shell: zsh\n"
        "---"
    )
    _write_skill(tmp_path / "proj" / ".agent" / "skills", "demo", fm)
    loader = _make_loader(tmp_path)
    spec = loader.get("demo")
    assert spec is not None
    assert spec.name == "demo"
    assert spec.description == "演示"
    assert spec.when_to_use == "当需要时"
    assert spec.arguments == ["file", "lang"]
    assert spec.argument_hint == "<file> <lang>"
    assert spec.disable_model_invocation is True
    assert spec.user_invocable is False
    assert spec.allowed_tools == ["read", "grep"]
    assert spec.disallowed_tools == ["write"]
    assert spec.model == "deepseek-chat"
    assert spec.effort == "high"
    assert spec.context == "fork"
    assert spec.agent == "explore"
    assert spec.paths == ["*.py", "src/**"]
    assert spec.shell == "zsh"


def test_spec_parses_snake_and_kebab_frontmatter(tmp_path: Path):
    # 用 snake_case 写法也应可解析
    fm = (
        "---\n"
        "name: sn\n"
        "description: s\n"
        "disable_model_invocation: true\n"
        "user_invocable: false\n"
        "allowed_tools: [read]\n"
        "---"
    )
    _write_skill(tmp_path / "proj" / ".agent" / "skills", "sn", fm)
    loader = _make_loader(tmp_path)
    spec = loader.get("sn")
    assert spec is not None
    assert spec.disable_model_invocation is True
    assert spec.user_invocable is False
    assert spec.allowed_tools == ["read"]


def test_frontmatter_parse_error_degrades(tmp_path: Path):
    # 非法 YAML → 降级为空元数据，skill 仍存在
    d = tmp_path / "proj" / ".agent" / "skills" / "bad"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---: : :\n\t- x\nname: bad\ndescription: b\n---\nbody\n", encoding="utf-8")
    loader = _make_loader(tmp_path)
    spec = loader.get("bad")
    assert spec is not None
    assert spec.description == ""  # 降级后无 description


# --------------------------------------------------------------------------- #
# 触发目录 / 不变量
# --------------------------------------------------------------------------- #
def test_catalog_prompt_only_name_and_trigger(tmp_path: Path):
    _write_skill(tmp_path / "proj" / ".agent" / "skills", "a",
                 "---\nname: a\ndescription: 触发A\n---",
                 body="这是不应出现的长正文内容")
    loader = _make_loader(tmp_path)
    catalog = loader.catalog_prompt()
    assert "触发A" in catalog
    assert "a:" in catalog
    # 不含正文
    assert "这是不应出现的长正文内容" not in catalog


def test_invariants_no_body_in_discover(tmp_path: Path):
    _write_skill(tmp_path / "proj" / ".agent" / "skills", "a",
                 "---\nname: a\ndescription: A\n---",
                 body="正文绝不进 discover 上下文")
    loader = _make_loader(tmp_path)
    _ = loader.discover()
    # discover 之后，skill 的 body 仍未读取（缓存为 None）
    spec = loader.get("a")
    assert spec is not None
    assert spec._body_cache is None
    # 仅当显式调用 body/render 才读取
    assert "正文绝不进 discover 上下文" in spec.body()


# --------------------------------------------------------------------------- #
# 正文按需加载
# --------------------------------------------------------------------------- #
def test_body_on_demand_and_cached(tmp_path: Path):
    _write_skill(tmp_path / "proj" / ".agent" / "skills", "a",
                 "---\nname: a\ndescription: A\n---", body="HELLO BODY")
    loader = _make_loader(tmp_path)
    spec = loader.get("a")
    assert spec is not None
    assert spec._body_cache is None
    assert spec.body() == "HELLO BODY"
    assert spec._body_cache == "HELLO BODY"
    # 第二次读应与第一次一致（缓存）
    assert spec.body() == "HELLO BODY"


# --------------------------------------------------------------------------- #
# 参数替换
# --------------------------------------------------------------------------- #
def test_render_body_arguments_replacement(tmp_path: Path):
    _write_skill(tmp_path / "proj" / ".agent" / "skills", "a",
                 "---\nname: a\ndescription: A\n---",
                 body="参数汇总：$ARGUMENTS")
    loader = _make_loader(tmp_path)
    spec = loader.get("a")
    assert spec is not None
    out = spec.render_body(["x", "y"])
    assert out == "参数汇总：x y"


def test_render_body_index_args(tmp_path: Path):
    _write_skill(tmp_path / "proj" / ".agent" / "skills", "a",
                 "---\nname: a\ndescription: A\n---",
                 body="第一=$0 第二=$1 越界=$5")
    loader = _make_loader(tmp_path)
    spec = loader.get("a")
    assert spec is not None
    out = spec.render_body(["alpha", "beta"])
    assert out == "第一=alpha 第二=beta 越界=$5\n\nARGUMENTS: alpha beta"


def test_render_body_arguments_n_index(tmp_path: Path):
    _write_skill(tmp_path / "proj" / ".agent" / "skills", "a",
                 "---\nname: a\ndescription: A\n---",
                 body="取=$ARGUMENTS[1]")
    loader = _make_loader(tmp_path)
    spec = loader.get("a")
    assert spec is not None
    out = spec.render_body(["zero", "one", "two"])
    assert out == "取=one"


def test_render_body_named_args(tmp_path: Path):
    _write_skill(tmp_path / "proj" / ".agent" / "skills", "a",
                 "---\nname: a\ndescription: A\narguments: [file, lang]\n---",
                 body="文件=$file 语言=$lang")
    loader = _make_loader(tmp_path)
    spec = loader.get("a")
    assert spec is not None
    out = spec.render_body(named={"file": "main.py", "lang": "py"})
    assert out == "文件=main.py 语言=py"


def test_render_body_skill_dir(tmp_path: Path):
    d = _write_skill(tmp_path / "proj" / ".agent" / "skills", "a",
                     "---\nname: a\ndescription: A\n---",
                     body="DIR=${SKILL_DIR}")
    loader = _make_loader(tmp_path)
    spec = loader.get("a")
    assert spec is not None
    out = spec.render_body()
    assert out == f"DIR={d.resolve()}"


def test_render_body_escape_dollar(tmp_path: Path):
    _write_skill(tmp_path / "proj" / ".agent" / "skills", "a",
                 "---\nname: a\ndescription: A\n---",
                 body=r"价格=\$5 占位=$ARGUMENTS")
    loader = _make_loader(tmp_path)
    spec = loader.get("a")
    assert spec is not None
    out = spec.render_body(["v"])
    assert out == "价格=$5 占位=v"


def test_render_body_append_arguments_when_no_token(tmp_path: Path):
    _write_skill(tmp_path / "proj" / ".agent" / "skills", "a",
                 "---\nname: a\ndescription: A\n---",
                 body="无参数 token 的正文")
    loader = _make_loader(tmp_path)
    spec = loader.get("a")
    assert spec is not None
    out = spec.render_body(["extra"])
    assert out == "无参数 token 的正文\n\nARGUMENTS: extra"


# --------------------------------------------------------------------------- #
# 自动启用判定
# --------------------------------------------------------------------------- #
def test_is_auto_enabled_disable_model_invocation(tmp_path: Path):
    spec = SkillSpec(name="x", description="d", path=tmp_path, disable_model_invocation=True)
    loader = _make_loader(tmp_path)
    assert loader.is_auto_enabled(spec) is False
    assert loader.is_auto_enabled(spec, current_file="a.py") is False


def test_is_auto_enabled_default_true(tmp_path: Path):
    spec = SkillSpec(name="x", description="d", path=tmp_path)
    loader = _make_loader(tmp_path)
    assert loader.is_auto_enabled(spec) is True
    # 无 paths 时 current_file 不影响
    assert loader.is_auto_enabled(spec, current_file="any.py") is True


def test_is_auto_enabled_paths_glob_match(tmp_path: Path):
    spec = SkillSpec(name="x", description="d", path=tmp_path, paths=["*.py"])
    loader = _make_loader(tmp_path)
    assert loader.is_auto_enabled(spec, current_file="src/main.py") is True
    assert loader.is_auto_enabled(spec, current_file="main.py") is True
    assert loader.is_auto_enabled(spec, current_file="main.md") is False
    # 有 paths 但无 current_file → False
    assert loader.is_auto_enabled(spec) is False


def test_is_auto_enabled_paths_dstar_glob(tmp_path: Path):
    spec = SkillSpec(name="x", description="d", path=tmp_path, paths=["src/**/*.py"])
    loader = _make_loader(tmp_path)
    assert loader.is_auto_enabled(spec, current_file="src/a/b/c.py") is True
    assert loader.is_auto_enabled(spec, current_file="other/x.py") is False


# --------------------------------------------------------------------------- #
# trigger_text 截断
# --------------------------------------------------------------------------- #
def test_trigger_text_truncated(tmp_path: Path):
    long = "x" * 2000
    spec = SkillSpec(name="x", description=long, path=tmp_path)
    assert len(spec.trigger_text) == 1536
