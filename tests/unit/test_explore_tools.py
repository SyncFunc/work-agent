"""M11.6 验收：探索工具（glob/find/list_dir/fetch_url）行为，不碰真实文件系统根。"""

from pathlib import Path

import pytest

from agent.runtime.registry import default_registry
from agent.tools import explore  # noqa: F401  (导入触发 glob/find/list_dir/fetch_url 注册，副作用)


@pytest.fixture
def tmp_root(tmp_path: Path, monkeypatch):
    """把进程 cwd 指向临时目录，使工具的相对路径解析隔离在 tmp_path。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


async def _setup_tree(tmp_root: Path):
    (tmp_root / "src").mkdir()
    (tmp_root / "src" / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    (tmp_root / "src" / "utils.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_root / "README.md").write_text("# readme\n", encoding="utf-8")
    (tmp_root / "src" / "nested").mkdir()
    (tmp_root / "src" / "nested" / "deep.py").write_text("y = 2\n", encoding="utf-8")


async def test_glob_recursive_py(tmp_root: Path):
    await _setup_tree(tmp_root)
    r = await default_registry.run("glob", {"pattern": "**/*.py"})
    assert r.ok, r.error
    assert "src/main.py" in r.output
    assert "src/utils.py" in r.output
    assert "src/nested/deep.py" in r.output


async def test_glob_single_star_and_nomatch(tmp_root: Path):
    await _setup_tree(tmp_root)
    r = await default_registry.run("glob", {"pattern": "*.md"})
    assert r.ok
    assert "README.md" in r.output
    r2 = await default_registry.run("glob", {"pattern": "**/*.xyz"})
    assert r2.ok and "no matches" in r2.output


async def test_glob_base_dir(tmp_root: Path):
    await _setup_tree(tmp_root)
    r = await default_registry.run("glob", {"pattern": "**/*.py", "base": "src"})
    assert r.ok
    assert "src/main.py" in r.output  # 输出仍相对工作根
    assert "src/nested/deep.py" in r.output


async def test_glob_rejects_escape(tmp_root: Path):
    await _setup_tree(tmp_root)
    r = await default_registry.run("glob", {"pattern": "**", "base": "../../"})
    assert not r.ok and "escapes root" in r.error


async def test_find_by_name(tmp_root: Path):
    await _setup_tree(tmp_root)
    r = await default_registry.run("find", {"name": "main"})
    assert r.ok, r.error
    assert "src/main.py" in r.output


async def test_find_by_ext(tmp_root: Path):
    await _setup_tree(tmp_root)
    r = await default_registry.run("find", {"ext": "py"})
    assert r.ok
    assert "src/main.py" in r.output
    assert "src/nested/deep.py" in r.output


async def test_find_no_match(tmp_root: Path):
    await _setup_tree(tmp_root)
    r = await default_registry.run("find", {"name": "zzz"})
    assert r.ok and "no files match" in r.output


async def test_find_requires_name_or_ext(tmp_root: Path):
    await _setup_tree(tmp_root)
    r = await default_registry.run("find", {})
    assert not r.ok and "at least one" in r.error


async def test_list_dir_shows_entries(tmp_root: Path):
    await _setup_tree(tmp_root)
    r = await default_registry.run("list_dir", {"path": ""})
    assert r.ok, r.error
    assert "src/" in r.output  # 目录带 /
    assert "README.md" in r.output
    assert "B" in r.output  # 大小后缀


async def test_list_dir_non_dir_fails(tmp_root: Path):
    await _setup_tree(tmp_root)
    r = await default_registry.run("list_dir", {"path": "README.md"})
    assert not r.ok and "not a directory" in r.error


async def test_fetch_url_rejects_bad_scheme(tmp_root: Path):
    r = await default_registry.run("fetch_url", {"url": "file:///etc/passwd"})
    assert not r.ok and "unsupported scheme" in r.error


async def test_fetch_url_missing_url(tmp_root: Path):
    r = await default_registry.run("fetch_url", {})
    assert not r.ok and r.error
