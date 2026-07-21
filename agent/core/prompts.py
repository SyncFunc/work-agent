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

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from jinja2 import StrictUndefined, Template

from agent.runtime.sandbox import SandboxProfile

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


# --------------------------------------------------------------------------- #
# M4.5：System Prompt 静态段 / 动态段分离 + 固定底座（AGENTS.md）
# --------------------------------------------------------------------------- #
def _read_agents_md(settings) -> str | None:
    """读取项目级 ``AGENTS.md`` 固定底座内容（永不压缩，每次投影重新注入）。

    位置优先级（高 → 低）：
        1) 项目根 ``<AGENT_PROJECT_ROOT>/<agents_md_path>``（用户可编辑，应 commit）
        2) 项目级 ``<AGENT_PROJECT_ROOT>/.agent/AGENTS.md``（自动维护）
        3) 用户级 ``<AGENT_USER_CONFIG_DIR or ~/.agent>/AGENTS.md``

    ``settings.context.agents_md_enabled`` 为 False 时直接返回 None。
    """
    if not getattr(settings, "context", None) or not settings.context.agents_md_enabled:
        return None
    root = Path(os.environ.get("AGENT_PROJECT_ROOT") or Path.cwd())
    user_root = Path(os.environ.get("AGENT_USER_CONFIG_DIR") or Path.home() / ".agent")
    rel = settings.context.agents_md_path or "AGENTS.md"
    candidates = [
        root / rel,
        root / ".agent" / "AGENTS.md",
        user_root / "AGENTS.md",
    ]
    for p in candidates:
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8").strip()
        except Exception:
            continue
    return None


def _build_dynamic_segment(settings) -> str:
    """构建 System Prompt 动态段（每轮更新，不进 prompt cache）。

    含：当前日期（外移出静态段以改善缓存命中）、AGENTS.md 固定底座、
    以及 Git 仓库状态（分支 + 简短 status）。
    """
    parts: list[str] = []
    # 日期：移出静态段，避免每轮变化破坏稳定前缀的缓存命中。
    parts.append(f"## 当前日期\n{date.today().isoformat()}\n")

    # AGENTS.md 固定底座：永不压缩，每次从磁盘重新读取注入首条 <system-reminder>。
    if settings.context.agents_md_enabled:
        agents_content = _read_agents_md(settings)
        if agents_content:
            parts.append(
                f"<system-reminder>\n## AGENTS.md\n{agents_content}\n</system-reminder>\n"
            )

    # Git 仓库状态（最佳努力，失败静默跳过）。
    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        status = subprocess.check_output(
            ["git", "status", "--short"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        if branch or status:
            parts.append(f"## Git 状态\n分支：{branch}\n{status[:1000]}\n")
    except Exception:
        pass

    return "\n".join(parts)


def _build_system_parts(
    settings,
    *,
    plan_mode: bool = False,
    has_plan: bool = False,
    clarify_enabled: bool = True,
    skills_catalog: str = "",
    agents_catalog: str = "",
) -> tuple[str, str]:
    """构建静态段与动态段（分别返回），供 ``build_system_prompt`` 拼接，也供上下文计量使用。"""
    try:
        _net_allowed = SandboxProfile(settings.sandbox.profile) == SandboxProfile.DANGER_FULL
    except ValueError:
        _net_allowed = False
    static = load_prompt("system").render(
        clarify_enabled=clarify_enabled,
        plan_mode=plan_mode,
        has_plan=has_plan,
        sandbox_profile=settings.sandbox.profile,
        approval_mode=settings.approval.mode,
        network_allowed=_net_allowed,
        sandbox_exec_policy=list(settings.approval.exec_policy),
        skills_catalog=skills_catalog,
        agents_catalog=agents_catalog,
    )
    dynamic = _build_dynamic_segment(settings)
    return static, dynamic


def build_system_prompt(
    settings,
    *,
    plan_mode: bool = False,
    has_plan: bool = False,
    clarify_enabled: bool = True,
    skills_catalog: str = "",
    agents_catalog: str = "",
) -> str:
    """构建完整 System Prompt（静态段 + 动态段），稳定前缀在前以复用 prompt cache。

    静态段来自 ``system.md`` 渲染（身份 / 安全约束 / 工具规范 / 模式指引 / skill·agent 目录），
    动态段（日期 / AGENTS.md / Git 状态）追加其后且每轮重新生成。
    """
    static, dynamic = _build_system_parts(
        settings,
        plan_mode=plan_mode,
        has_plan=has_plan,
        clarify_enabled=clarify_enabled,
        skills_catalog=skills_catalog,
        agents_catalog=agents_catalog,
    )
    if dynamic:
        return static + "\n\n" + dynamic
    return static
