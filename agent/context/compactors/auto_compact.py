"""Auto Compact 压缩器（M4.3，9 段摘要 + 失败断路器）。

当 Microcompact 后上下文仍超阈值时，把 ``compact_boundary`` 之前的历史整批压缩为
**9 段结构化摘要**（1 次模型调用），然后插入 Compact Boundary 标记。

设计要点（见 milestones/M4.../4.3）：
- 提取 boundary 前的完整历史作为压缩源。
- 发送压缩 prompt + 历史给 LLM（model.act）。
- 解析输出中 <summary>...</summary> 内容。
- 用摘要替换 boundary 前历史，保留 boundary 后原文。
- 连续失败 > max_failures 次则放弃（防止无限重试浪费 token）。
"""

from __future__ import annotations

from agent.core.model import Message, Model
from agent.obs.tracer import Tracer, _span

# 内联 prompt（模板文件在 agent/prompts/compact_*.md，供调试/审计参考）
COMPACT_SYSTEM_PROMPT = (
    "你是一个上下文压缩助手。你的任务是把一段对话历史压缩成结构化摘要，"
    "保留关键信息，丢弃冗余细节。只输出文本，不要调用任何工具。"
    "输出格式：<summary>...</summary>。"
)

COMPACT_USER_TEMPLATE = """请把以下对话历史压缩为 9 段结构化摘要：

1. **Primary Request and Intent** — 用户的原始请求和意图
2. **Key Technical Concepts** — 涉及的关键技术概念
3. **Files and Code Sections** — 涉及的文件路径和代码段
4. **Errors and Fixes** — 遇到的错误和修复方法
5. **Problem Solving** — 解决问题的思路和步骤
6. **All User Messages** — 所有用户消息的关键内容
7. **Pending Tasks** — 尚未完成的待办事项
8. **Current Work** — 当前正在进行的任务细节（包含原文引用）
9. **Optional Next Step** — 建议的下一步操作（包含原文引用）

对话历史：
{history_text}

请输出 <summary>...</summary> 格式的结果。第 8、9 段必须包含原文引用以防止语义漂移。"""


class AutoCompact:
    """Auto Compact 压缩器：1 次模型调用，生成 9 段结构化摘要。

    策略：
    - 提取 boundary 前的完整历史作为压缩源。
    - 发送压缩 prompt + 历史给 LLM（model.act）。
    - 解析输出中 <summary>...</summary> 内容。
    - 用摘要替换 boundary 前历史，保留 boundary 后原文。
    - 连续失败 > max_failures 次则放弃（防止无限重试浪费 token）。
    """

    def __init__(
        self,
        model: Model,
        max_failures: int = 3,
        tracer: Tracer | None = None,
        recent_keep: int = 8,
    ) -> None:
        self.model = model
        self.max_failures = max_failures
        self.failure_count = 0
        self.tracer = tracer
        # 首次压缩（boundary=0）时保留的最近消息条数：压缩更早的历史，保留最近上下文。
        self.recent_keep = max(1, recent_keep)

    async def compact(self, conv: list[Message], boundary: int) -> list[Message]:
        """把 boundary 之前的历史压缩为摘要，返回替换后的消息列表。

        修复：``boundary <= 0`` 时（首次压缩，尚无已压缩边界）不再直接 no-op，
        而是自动取 ``len(conv) - recent_keep`` 作为压缩点，压缩更早的历史、保留最近
        ``recent_keep`` 条消息，使第一次超阈值也能真正生成摘要。
        """
        with _span(self.tracer, "compact.auto_compact", kind="compact") as ac_span:
            if ac_span is not None:
                ac_span.log("boundary", boundary)
                ac_span.log("conv_len", len(conv))
            # 首次压缩：boundary<=0 表示尚无「已压缩边界」，自动切分保留最近上下文。
            if boundary <= 0:
                if len(conv) <= self.recent_keep:
                    if ac_span is not None:
                        ac_span.log("skip", "conv 过短无需压缩，原样返回", level="warn")
                    return conv
                boundary = max(1, len(conv) - self.recent_keep)
            if boundary > len(conv):
                if ac_span is not None:
                    ac_span.log("skip", "boundary 越界，原样返回", level="warn")
                return conv
            if self.failure_count >= self.max_failures:
                if ac_span is not None:
                    ac_span.log("skip", "失败断路器已触发，放弃压缩", level="warn")
                return conv

            history = conv[:boundary]
            history_text = self._format_history(history)

            prompt = COMPACT_USER_TEMPLATE.format(history_text=history_text)
            summary = await self._call_model(prompt)

            if summary is None:
                self.failure_count += 1
                if ac_span is not None:
                    ac_span.log("result", "模型调用失败，断路器 +1", level="warn")
                return conv

            self.failure_count = 0
            if ac_span is not None:
                ac_span.log("summary_len", len(summary))
            # 用摘要替换 boundary 前历史
            return [
                Message(role="user", content=f"[Compact Summary]\n{summary}"),
            ] + conv[boundary:]

    async def _call_model(self, prompt: str) -> str | None:
        """调用模型获取摘要。返回 <summary> 标签内文本，失败返回 None。"""
        messages = [
            Message(role="system", content=COMPACT_SYSTEM_PROMPT),
            Message(role="user", content=prompt),
        ]
        try:
            with _span(self.tracer, "model.act", kind="model") as mspan:
                if mspan is not None:
                    mspan.log("prompt_len", len(prompt))
                decision = await self.model.act(messages)
                if mspan is not None:
                    if decision.usage:
                        mspan.meta["usage"] = decision.usage
                    mspan.log("text_len", len(decision.text or ""))
            text = decision.text or ""
            if "<summary>" in text and "</summary>" in text:
                start = text.index("<summary>") + len("<summary>")
                end = text.index("</summary>")
                return text[start:end].strip()
            # 无标签时兜底取全文
            return text.strip() if text else None
        except Exception:
            return None

    def _format_history(self, messages: list[Message]) -> str:
        """把 messages 格式化为可读文本。"""
        lines: list[str] = []
        for i, msg in enumerate(messages):
            role = msg.role
            content = msg.content or ""
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    content += f"\n  → tool: {tc.name}({tc.arguments})"
            lines.append(f"[{i}][{role}] {content[:2000]}")
        return "\n".join(lines)


# 协议检查：AutoCompact 满足 Compactor 协议
# isinstance(AutoCompact(...), Compactor) → True
