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
        """把除最近 ``recent_keep`` 条外的全部历史压缩为摘要，返回替换后的消息列表。

        实际压缩点恒为 ``cut = max(1, len(conv) - recent_keep)``：始终保留最近
        ``recent_keep`` 条原文，压缩更早的历史。``boundary`` 仅用于日志与越界提示，
        **不再截断压缩点**。

        这样设计是为了消除「锯齿效应」：旧实现里 ``mark_boundary`` 会把本轮保留的
        活跃区也标入「已压边界之前」，导致下一轮 ``AutoCompact`` 只压掉很小的
        ``[summary] + 上一轮保留区``、却把本轮新增全部留作巨大的保留区；再下一轮
        保留区早已爆满、触发巨量压缩。改为「每轮恒留最近 ``recent_keep`` 条」后，
        压缩量稳定、保留区恒定，不再锯齿。

        注意：压缩点在 ``cut``，``conv[cut:]`` 整体保留。调用方需保证 ``cut`` 不落在
        ``tool_use`` / ``tool_result`` 配对中间（否则会孤立任一方）；本实现沿用上层
        约定，不在此处拆分配对。
        """
        with _span(self.tracer, "compact.auto_compact", kind="compact") as ac_span:
            if ac_span is not None:
                ac_span.log("boundary", boundary)
                ac_span.log("conv_len", len(conv))
            # 太短：不足以留出 recent_keep 条保留，原样返回。
            if len(conv) <= self.recent_keep:
                if ac_span is not None:
                    ac_span.log("skip", "conv 过短无需压缩，原样返回", level="warn")
                return conv
            if self.failure_count >= self.max_failures:
                if ac_span is not None:
                    ac_span.log("skip", "失败断路器已触发，放弃压缩", level="warn")
                return conv

            # 实际压缩点：恒留最近 recent_keep 条，与传入的 boundary 无关。
            cut = max(1, len(conv) - self.recent_keep)
            history = conv[:cut]
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
                ac_span.log("cut", cut)
            # 用摘要替换 cut 之前的历史，保留 cut 之后的原文
            return [
                Message(role="user", content=f"[Compact Summary]\n{summary}"),
            ] + conv[cut:]

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
