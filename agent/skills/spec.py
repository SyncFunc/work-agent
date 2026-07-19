"""Skill 定义（M5.1）。

``SkillSpec`` 描述一个 Skill：触发描述（常驻、低成本）与按需加载的正文。
正文（SKILL.md 除 frontmatter 外的部分）**不在 discover 阶段进入上下文**，
仅在模型决定调用时才由 ``render_body`` 读取并做参数替换。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from agent.core.prompts import _split_frontmatter

_TRIGGER_MAX = 1536
_ESCAPE = "\x00ESC\x00"  # 转义占位符，避免与参数替换冲突


@dataclass
class SkillSpec:
    """一个 Skill 的静态定义（来自 ``<dir>/SKILL.md`` 的 frontmatter）。"""

    name: str                       # 目录名（kebab-case）
    description: str                # 触发描述（常驻）
    path: Path                      # SKILL.md 所在目录
    when_to_use: str = ""           # 额外触发上下文
    arguments: list[str] = field(default_factory=list)   # 命名位置参数
    argument_hint: str = ""
    disable_model_invocation: bool = False   # True→仅 /name 手动
    user_invocable: bool = True
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    model: str | None = None        # 活动时覆盖模型（"inherit" 或模型 id）
    effort: str | None = None
    context: str | None = None      # "fork" → 在 subagent 中执行
    agent: str | None = None        # context:fork 时用的 subagent 类型
    hooks: list[dict] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)   # Glob，限定自动触发文件
    shell: str = "bash"

    _body_cache: str | None = field(default=None, repr=False, compare=False)

    @property
    def trigger_text(self) -> str:
        """常驻触发描述：description + when_to_use（截断 1536 字符）。"""
        t = self.description
        if self.when_to_use:
            t = f"{t}\n何时使用：{self.when_to_use}"
        return t[:_TRIGGER_MAX]

    def body(self) -> str:
        """按需读取 SKILL.md 正文（不含 frontmatter）。首次调用才读文件并缓存。"""
        if self._body_cache is None:
            p = self.path / "SKILL.md"
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                self._body_cache = ""
            else:
                _, body = _split_frontmatter(text)
                self._body_cache = body.rstrip("\n")
        return self._body_cache

    def render_body(
        self,
        args: list[str] | None = None,
        named: dict[str, str] | None = None,
    ) -> str:
        """参数替换后返回完整正文。

        替换规则：
        - ``$ARGUMENTS`` → 全部参数串；正文不含该 token 但有参数时，追加 ``ARGUMENTS: <value>``。
        - ``$ARGUMENTS[N]`` / ``$N`` → 0 基索引参数。
        - ``$name`` → ``arguments`` 声明的命名参数（由 ``named`` 提供）。
        - ``${SKILL_DIR}`` → ``spec.path`` 绝对路径（脚本引用用）。
        - 转义：``\\$`` 保留文字 ``$``。
        """
        args = list(args or [])
        named = dict(named or {})
        raw = self.body()
        had_arguments_token = "$ARGUMENTS" in raw

        text = raw.replace("${SKILL_DIR}", str(self.path.resolve()))

        # 先保护转义字符，避免被当作参数 token
        text = text.replace("\\$", _ESCAPE)

        def _repl_index(m: re.Match) -> str:
            idx = int(m.group(1))
            return args[idx] if 0 <= idx < len(args) else m.group(0)

        # 索引：$ARGUMENTS[N] 优先；其次裸 $N
        text = re.sub(r"\$ARGUMENTS\[(\d+)\]", _repl_index, text)
        text = re.sub(r"\$(\d+)", _repl_index, text)

        if had_arguments_token:
            text = text.replace("$ARGUMENTS", " ".join(args))
        elif args:
            text = text.rstrip() + f"\n\nARGUMENTS: {' '.join(args)}"

        # 命名参数：$name（仅在 arguments 声明且 named 提供时）
        for nm in self.arguments:
            if nm in named:
                text = re.sub(r"\$" + re.escape(nm) + r"\b", named[nm], text)

        text = text.replace(_ESCAPE, "$")
        return text
