"""提示词加载（代码与提示词分离，主流的「Markdown + frontmatter + 模板」结构）。

设计：
- 提示词是**结构化文件**：`<包>/prompts/<name>.md`，含 YAML frontmatter（元数据）+ Markdown 正文（模板）。
- 正文用 Jinja2 渲染，支持 `{{ var }}` 变量与 `{% if %}` 等控制结构；未提供变量时报错（StrictUndefined）。
- 渲染所需变量由调用方（loop）按运行时状态注入（clarify_enabled / plan_mode / has_plan …）。

frontmatter 约定字段：
- name:        提示词标识
- description: 用途说明
- version:     版本号（整数）
- variables:   模板引用的变量名列表（仅文档/校验用途，渲染时由 render(**vars) 提供）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import StrictUndefined, Template

# 包内 prompts 目录：agent/prompts/（随包发布，hatchling 打包 agent 时一并包含）
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


@dataclass
class Prompt:
    """渲染后的提示词句柄。"""

    name: str
    description: str
    version: int
    variables: list[str]
    body: str
    _template: Template = field(repr=False)

    def render(self, **vars: Any) -> str:
        """用运行时变量渲染模板；未提供的变量会触发 StrictUndefined 报错。"""
        return self._template.render(**vars).strip()


def load_prompt(name: str, base_dir: Path | None = None) -> Prompt:
    """从 `base_dir/<name>.md`（默认包内 prompts/）加载并解析一个提示词。"""
    base = base_dir or _PROMPTS_DIR
    path = base / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    meta_raw, body = _split_frontmatter(text)
    meta = yaml.safe_load(meta_raw) or {}
    template = Template(body, undefined=StrictUndefined)
    return Prompt(
        name=str(meta.get("name", name)),
        description=str(meta.get("description", "")),
        version=int(meta.get("version", 1)),
        variables=list(meta.get("variables", []) or []),
        body=body,
        _template=template,
    )


def _split_frontmatter(text: str) -> tuple[str, str]:
    """分离 YAML frontmatter 与正文。无 frontmatter 时返回 ("", text)。"""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return "", text
    return m.group(1), m.group(2)
