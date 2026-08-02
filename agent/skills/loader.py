"""SkillLoader（M5.1）。

从项目级 ``<project>/.agent/skills/<name>/SKILL.md`` 与用户级
``<user_root>/skills/<name>/SKILL.md`` 发现并解析 Skill。**项目级 > 用户级**（同名覆盖）。

设计：
- 触发描述（``trigger_text``）常驻，由 ``catalog_prompt`` 注入系统提示（低成本）。
- 正文**按需加载**：``discover`` 阶段不读 SKILL.md 正文，仅在模型调用时由
  ``SkillSpec.render_body`` 读取并做参数替换。
- 自动触发判定（``is_auto_enabled``）：``disable-model-invocation`` → False；
  ``paths`` 设 Glob → 仅匹配当前文件返回 True；否则 True。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agent.core.prompts import _split_frontmatter
from agent.skills.spec import SkillSpec


@dataclass
class SkillSummary:
    """Skill 的精简展示信息（供 CLI / catalog 渲染，不含正文）。"""

    name: str
    description: str
    paths: list[str]
    user_invocable: bool
    disable_model_invocation: bool
    source: str = "user"  # M11.6 来源：builtin / user / project（用于面板分组展示）


def _meta_get(meta: dict[str, Any], key: str, default: Any = None) -> Any:
    """读取 frontmatter 字段，兼容 snake_case 与 kebab-case 两种写法。"""
    if key in meta:
        return meta[key]
    kebab = key.replace("_", "-")
    if kebab in meta:
        return meta[kebab]
    return default


def _as_str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [str(v)]


class SkillLoader:
    def __init__(self, project_root: Path, user_root: Path | None = None) -> None:
        self.project_root = Path(project_root)
        self.user_root = Path(user_root) if user_root else (Path.home() / ".agent")
        self._project_dir = self.project_root / ".agent" / "skills"
        self._user_dir = self.user_root / "skills"
        # 内建 skill 目录：随代码分发，任何项目开箱即用。
        self._builtin_dir = Path(__file__).resolve().parent / "builtin"
        self._cache: dict[str, SkillSpec] | None = None

    # ------------------------------------------------------------------ #
    # 发现 / 获取
    # ------------------------------------------------------------------ #
    def discover(self) -> list[SkillSpec]:
        """扫描内建 + 项目级 + 用户级 skills；后扫描覆盖先扫描（项目覆盖用户覆盖内建）。"""
        skills: dict[str, SkillSpec] = {}
        for d, source in (
            (self._builtin_dir, "builtin"),  # 最低优先级：随代码分发
            (self._user_dir, "user"),
            (self._project_dir, "project"),  # 最高优先级
        ):
            if d.is_dir():
                for sub in sorted(d.iterdir()):
                    if sub.is_dir():
                        spec = self._parse_skill_dir(sub)
                        if spec is not None:
                            spec.source = source
                            skills[spec.name] = spec
        self._cache = skills
        return list(skills.values())

    def get(self, name: str) -> SkillSpec | None:
        if self._cache is None:
            self.discover()
        assert self._cache is not None
        return self._cache.get(name)

    def _parse_skill_dir(self, dir_path: Path) -> SkillSpec | None:
        """解析一个 skill 目录（须含 SKILL.md）。"""
        skill_md = dir_path / "SKILL.md"
        if not skill_md.is_file():
            return None
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError:
            return None
        meta_raw, _body = _split_frontmatter(text)
        try:
            meta: dict[str, Any] = yaml.safe_load(meta_raw) if meta_raw else {}
        except yaml.YAMLError:
            meta = {}  # 解析异常降级：空元数据
        if not isinstance(meta, dict):
            meta = {}

        name = str(meta.get("name") or dir_path.name)
        description = str(_meta_get(meta, "description", ""))
        when_to_use = str(_meta_get(meta, "when_to_use", ""))
        arguments = _as_str_list(_meta_get(meta, "arguments"))
        argument_hint = str(_meta_get(meta, "argument_hint", ""))
        disable_model_invocation = bool(_meta_get(meta, "disable_model_invocation", False))
        user_invocable = bool(_meta_get(meta, "user_invocable", True))
        allowed_tools = _as_str_list(_meta_get(meta, "allowed_tools"))
        disallowed_tools = _as_str_list(_meta_get(meta, "disallowed_tools"))
        model = _meta_get(meta, "model")
        effort = _meta_get(meta, "effort")
        context = _meta_get(meta, "context")
        agent = _meta_get(meta, "agent")
        hooks_raw = _meta_get(meta, "hooks") or []
        hooks = hooks_raw if isinstance(hooks_raw, list) else []
        paths = _as_str_list(_meta_get(meta, "paths"))
        shell = str(_meta_get(meta, "shell", "bash"))

        return SkillSpec(
            name=name,
            description=description,
            path=dir_path,
            when_to_use=when_to_use,
            arguments=arguments,
            argument_hint=argument_hint,
            disable_model_invocation=disable_model_invocation,
            user_invocable=user_invocable,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            model=model,
            effort=effort,
            context=context,
            agent=agent,
            hooks=hooks,
            paths=paths,
            shell=shell,
        )

    # ------------------------------------------------------------------ #
    # 触发目录 / 自动启用
    # ------------------------------------------------------------------ #
    def catalog_prompt(self) -> str:
        """返回注入系统提示的触发目录（仅 name + trigger_text，不含正文）。

        M11.6：只暴露「启用」的技能——``disable_model_invocation=True``（被关闭/仅手动）
        或 ``user_invocable=False`` 的技能**不注入**，模型感知不到，避免「关闭后仍能感知」。
        """
        if self._cache is None:
            self.discover()
        assert self._cache is not None
        lines = [
            f"- {spec.name}: {spec.trigger_text}"
            for spec in self._cache.values()
            if not spec.disable_model_invocation and spec.user_invocable
        ]
        return "\n".join(lines)

    def summaries(self) -> list[SkillSummary]:
        """M5.4：返回精简列表（name + 描述 + paths + 手动标志），不含正文。

        每次调用重新 ``discover()``（实时检测会话中新加的 skill 目录）。
        """
        return [
            SkillSummary(
                name=s.name,
                description=s.description,
                paths=list(s.paths),
                user_invocable=s.user_invocable,
                disable_model_invocation=s.disable_model_invocation,
                source=s.source,
            )
            for s in self.discover()
        ]

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """M11.6 技能开关：写回 SKILL.md frontmatter 的 disable_model_invocation。

        enabled=True → 模型可自动调用（disable_model_invocation=False）；
        enabled=False → 仅手动 /name（disable_model_invocation=True）。
        找不到 skill 或写回失败返回 False。
        """
        spec = self.get(name)
        if spec is None:
            return False
        skill_md = spec.path / "SKILL.md"
        if not skill_md.is_file():
            return False
        try:
            text = skill_md.read_text(encoding="utf-8")
            meta_raw, body = _split_frontmatter(text)
            meta = yaml.safe_load(meta_raw) if meta_raw else {}
            if not isinstance(meta, dict):
                meta = {}
            meta["disable_model_invocation"] = not enabled
            new_frontmatter = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
            skill_md.write_text(f"---\n{new_frontmatter}\n---\n{body}", encoding="utf-8")
            self._cache = None  # 失效缓存，下次 discover 重读
            return True
        except OSError:
            return False

    def is_auto_enabled(self, spec: SkillSpec, current_file: str | None = None) -> bool:
        """模型是否可自动触发该 skill（三重判定）：

        - ``disable-model-invocation=True`` → False（仅手动 ``/name``）；
        - ``paths`` 设 Glob → 仅当前文件匹配其一返回 True，否则 False；
        - 其余 → True。
        """
        if spec.disable_model_invocation:
            return False
        if spec.paths:
            if current_file is None:
                return False
            return self._matches_path(current_file, spec.paths)
        return True

    @staticmethod
    def _matches_path(current_file: str, globs: list[str]) -> bool:
        p = Path(current_file)
        for g in globs:
            if fnmatch.fnmatch(current_file, g) or fnmatch.fnmatch(p.name, g):
                return True
            # 兼容 ** 写法（fnmatch 仅支持 *）
            norm = g.replace("**/", "*").replace("/**", "/*").replace("**", "*")
            if norm != g and (fnmatch.fnmatch(current_file, norm) or fnmatch.fnmatch(p.name, norm)):
                return True
        return False
