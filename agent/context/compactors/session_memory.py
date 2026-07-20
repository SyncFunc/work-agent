"""Session Memory Compact 压缩器（M4.4，零成本首选）。

后台**增量维护**一个会话摘要文件（markdown，10 段固定结构），每次压缩时**优先复用**
这份摘要替代历史，零 API 调用。这是 Claude Code 的「零成本首选」方案，比 Auto Compact
（1 次 LLM 调用）更划算。

设计要点（见 milestones/M4-上下文与记忆/4.4-SessionMemoryCompact.md）：
- 摘要文件：``<session_memory_dir>/<session_id>/session-memory/summary.md``
  （目录 0o700 / 文件 0o600，含项目敏感信息）。
- 固定 10 段 section，每段 ≤2K tokens、整文件 ≤12K tokens。
- ``should_update()``：token 增量为必要条件；首次需达 init 阈值；触发后由隔离的
  「记忆子 agent」增量更新（本项目复用 M5.4.1 后台 Subagent 机制，见 agent/core/session.py）。
- ``compact()``：有摘要时直接用 ``[Session Summary]`` 消息替换 boundary 前历史，并保留
  最近原文（对齐 Claude Code DEFAULT_SM_COMPACT_CONFIG），零 API 调用。

接口契约（验收标准）：
- ``load() -> str | None``：未保存返回 ``None``，否则返回摘要原文。
- ``save(summary)``：写出摘要；目录 0o700、文件 0o600。
- ``should_update(conv_tokens, tokens_since_update, tool_calls_since_update, last_round_has_tool) -> bool``
- ``compact(conv, boundary, ...) -> list[Message] | None``：有摘要返回压缩结果，否则 ``None``。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent.context.tokens import _estimate_tokens
from agent.core.model import Message


# --------------------------------------------------------------------------- #
# 固定 10 段 section 模板
# --------------------------------------------------------------------------- #
SUMMARY_SECTIONS = [
    "Session Title",
    "Current State",
    "Task specification",
    "Files and Functions",
    "Workflow",
    "Errors & Corrections",
    "Codebase and System Documentation",
    "Learnings",
    "Key results",
    "Worklog",
]
SECTION_MAX_TOKENS = 2_000
SUMMARY_MAX_TOKENS = 12_000

# 记忆子 agent 的 system prompt（只产出 10 段摘要文本，绝不调用工具）
MEMORY_SYSTEM_PROMPT = """你是会话记忆维护器（Session Memory Keeper）。你被授权读取本会话的完整
对话历史，并维护一份**结构化会话摘要**。

要求：
1. 输出**仅**为一份 10 段固定 section 的 markdown 摘要，不要任何额外解释、前后缀或工具调用。
2. 严格遵循以下 10 段顺序（每段用 `## 段名` 作标题）：
""" + "\n".join(f"{i+1}. {s}" for i, s in enumerate(SUMMARY_SECTIONS)) + """
3. 每段控制在 2,000 tokens 以内；整份摘要不超过 12,000 tokens。丢弃冗余细节、保留关键信息
   （文件路径、决策理由、未完成任务、错误修复方法）。
4. 你会拿到「现有摘要」与「最新对话」。请**增量更新**：保留仍有效的内容，补充新进展，移除已过时信息。
5. 输出必须是可直接落盘的 markdown；不要使用 ```代码块``` 包裹整份摘要。
"""


@dataclass
class SessionMemoryData:
    """会话摘要文件内容（内存表示）。

    注：``summary.md`` 直接存储 ``summary`` 原文（满足 ``load() == 保存的 summary`` 契约）。
    版本号与统计信息写入同名 sidecar ``.meta.json``，便于后续演进与观测，不影响读取契约。
    """

    session_id: str
    created_at: float
    updated_at: float
    summary: str  # 结构化摘要文本（10 段 markdown 格式）
    version: int = 1
    stats: dict = field(default_factory=dict)  # 压缩次数/节省 token/提取次数


@dataclass
class SessionMemoryConfig:
    """Session Memory 配置（来自 settings.context）。"""

    session_memory_dir: str = ".agent/sessions"
    minimum_message_tokens_to_init: int = 10_000
    minimum_tokens_between_update: int = 5_000
    tool_calls_between_updates: int = 3
    enabled: bool = True  # 前置：要求 Auto Compact 开启


class SessionMemory:
    """Session Memory Compact 压缩器：零成本，复用后台增量维护的摘要文件。"""

    def __init__(self, config: SessionMemoryConfig, session_id: str = "") -> None:
        self.config = config
        self.session_id = session_id
        self._base = Path(config.session_memory_dir) / session_id / "session-memory"
        self._base.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._base, 0o700)  # 目录仅本人可读
        except OSError:
            pass  # 某些平台（如 Windows）chmod 受限，忽略

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #
    @property
    def _summary_path(self) -> Path:
        return self._base / "summary.md"

    @property
    def _meta_path(self) -> Path:
        return self._base / ".meta.json"

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #
    def load(self) -> str | None:
        """加载会话摘要。不存在或为空返回 None。"""
        if not self._summary_path.exists():
            return None
        text = self._summary_path.read_text(encoding="utf-8").strip()
        return text or None

    def save(self, summary: str, stats: dict | None = None) -> None:
        """保存/更新会话摘要（10 段 markdown）。目录 0o700、文件 0o600。"""
        now = time.time()
        version = 1
        created = now
        if self._meta_path.exists():
            try:
                meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
                version = int(meta.get("version", 1)) + 1
                created = float(meta.get("created_at", now))
            except (json.JSONDecodeError, ValueError, OSError):
                pass
        data = SessionMemoryData(
            session_id=self.session_id,
            created_at=created,
            updated_at=now,
            summary=summary,
            version=version,
            stats=stats or {},
        )
        self._summary_path.write_text(summary, encoding="utf-8")
        try:
            os.chmod(self._summary_path, 0o600)  # 文件仅本人可读
        except OSError:
            pass
        self._meta_path.write_text(
            json.dumps(
                {
                    "version": data.version,
                    "created_at": data.created_at,
                    "updated_at": data.updated_at,
                    "stats": data.stats,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def load_stats(self) -> dict:
        """读取 sidecar 元数据（version / stats 等），不存在返回空 dict。"""
        if not self._meta_path.exists():
            return {}
        try:
            return json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    # ------------------------------------------------------------------ #
    # 触发判断
    # ------------------------------------------------------------------ #
    def should_update(
        self,
        conv_tokens: int,
        tokens_since_update: int,
        tool_calls_since_update: int,
        last_round_has_tool: bool,
    ) -> bool:
        """后处理钩子判断：是否该增量更新摘要。token 增量为必要条件。"""
        if not self.config.enabled:
            return False
        if self.load() is None and conv_tokens < self.config.minimum_message_tokens_to_init:
            return False  # 初次需达到 init 阈值
        if tokens_since_update < self.config.minimum_tokens_between_update:
            return False
        if tool_calls_since_update >= self.config.tool_calls_between_updates:
            return True
        return not last_round_has_tool  # 自然对话断点

    # ------------------------------------------------------------------ #
    # 压缩
    # ------------------------------------------------------------------ #
    def compact(
        self,
        conv: list[Message],
        boundary: int,
        keep_recent_tokens: int = 10_000,
        min_recent_messages: int = 5,
        max_recent_tokens: int = 40_000,
    ) -> list[Message] | None:
        """Session Memory Compact：直接用摘要替代 boundary 前历史（瞬时，零 API 调用）。

        保留最近原文的约束（对齐 Claude Code DEFAULT_SM_COMPACT_CONFIG）：
        - 至少保留 keep_recent_tokens（默认 10K）tokens 原文
        - 至少保留 min_recent_messages（默认 5）条含文本消息
        - 至多保留 max_recent_tokens（默认 40K） tokens
        有摘要则返回压缩后 conv，否则返回 None（走 Auto Compact 兜底）。
        """
        summary = self.load()
        if summary is None:
            return None

        # 从 boundary 之后向前保留最近原文，满足最小保留约束
        recent: list[Message] = []
        recent_tokens = 0
        recent_msgs = 0
        for msg in reversed(conv[boundary:]):
            text = msg.content or ""
            t = _estimate_tokens(text)
            reached_min = recent_msgs >= min_recent_messages and recent_tokens >= keep_recent_tokens
            if recent_tokens + t > max_recent_tokens or (
                reached_min and recent_tokens + t > keep_recent_tokens
            ):
                break
            recent.insert(0, msg)
            recent_tokens += t
            if text.strip():
                recent_msgs += 1

        return [Message(role="user", content=f"[Session Summary]\n{summary}")] + recent

    # ------------------------------------------------------------------ #
    # 提取任务构造（供复用 M5.4.1 后台 Subagent 的记忆子 agent 使用）
    # ------------------------------------------------------------------ #
    def build_extraction_task(self) -> str:
        """构造交给记忆子 agent 的 task 文本（含现有摘要，供增量更新）。"""
        existing = self.load()
        header = (
            "请基于本会话的完整对话历史，维护并更新会话摘要。"
            "严格输出 10 段固定 section 的 markdown（见你的系统提示）。"
        )
        if existing:
            return header + "\n\n## 现有摘要（请增量更新，保留有效内容、补充新进展）\n\n" + existing
        return header + "\n\n（当前尚无摘要，请基于对话历史生成首份 10 段摘要。）"
