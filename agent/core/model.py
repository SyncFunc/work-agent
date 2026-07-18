"""模型抽象层（provider 无关 + 支持流式）。

设计原则：
- AI 只做「决策」：给定 messages，产出 Decision（最终文本 或 工具调用）。
- 模型可插拔且 provider 无关：底层走 OpenAI 兼容协议（/v1/chat/completions），
  任何兼容服务（DeepSeek、OpenAI、本地 vLLM 等）只需改配置，无需改代码。
- 测试用 FakeModel / RecordingModel，绝不依赖真实 API。
- 支持流式：stream() 增量产出文本，结束以 done 事件回传完整 Decision（含工具调用）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from agent.config.settings import Settings


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # 模型在调用时于 arguments 内动态返回的保留字段 ``_approval_request`` 会被适配器
    # 提升到这里并从 arguments 剥除（真实工具看不到）。无需在工具 schema 里声明。
    approval_request: bool = False


@dataclass
class Message:
    """对话中的一条消息。role ∈ {system, user, assistant, tool}。"""

    role: str
    content: str | None = None
    tool_calls: list[ToolCall] | None = None  # assistant 发出
    tool_call_id: str | None = None  # tool 回执对应哪次调用


@dataclass
class Decision:
    """模型一次决策：要么给最终文本，要么发起若干工具调用。"""

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    # token 用量（provider 在响应里给出；OpenAI 兼容的 usage 字段）。None 表示未统计。
    usage: dict[str, int] | None = None

    @property
    def is_final(self) -> bool:
        return not self.tool_calls


@dataclass
class StreamEvent:
    """流式事件：逐片文本 (type="text")，工具调用增量 (type="tool_call_delta")，或收尾 (type="done")。

    ``kind`` 区分文本性质：``"reasoning"``=模型思考过程，``"content"``=正式输出。
    仅 type="text" 时有效；type="done" 时忽略。

    ``tool_call_delta``：模型流式生成工具调用参数时的增量。``tc_index`` 区分同一次
    Decision 里的多个并行工具调用；``tc_args`` 是该调用**累计**的原始 arguments JSON
    字符串（可能不完整，仅供 UI 预览）；``tc_name``/``tc_id`` 已知时携带。
    """

    type: str  # "text" | "tool_call_delta" | "done"
    text: str | None = None
    decision: Decision | None = None
    kind: str | None = None  # "reasoning" | "content"（仅 text 事件）
    # tool_call_delta 专用
    tc_index: int | None = None
    tc_id: str | None = None
    tc_name: str | None = None
    tc_args: str | None = None  # 该工具调用累计的原始 arguments JSON 字符串（可能不完整）


# --------------------------------------------------------------------------- #
# 协议
# --------------------------------------------------------------------------- #
@runtime_checkable
class Model(Protocol):
    async def act(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Decision:
        """一次性返回决策（非流式）。tools 为可选的 OpenAI 兼容 function 工具清单。"""
        ...

    def stream(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> AsyncIterator[StreamEvent]:
        """流式返回：若干 text 事件 + 一个 done 事件。

        注意：实现为异步生成器，故协议里声明为返回 AsyncIterator 的普通方法
        （异步生成器调用后直接返回 AsyncIterator，本身不是可 await 的协程）。
        tools 含义同 act。
        """
        ...


# --------------------------------------------------------------------------- #
# OpenAI 兼容协议互转
# --------------------------------------------------------------------------- #
def _to_openai(m: Message) -> dict[str, Any]:
    if m.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": m.tool_call_id or "",
            "content": m.content or "",
        }
    msg: dict[str, Any] = {"role": m.role, "content": m.content or ""}
    if m.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in m.tool_calls
        ]
    return msg


def _from_openai(tc: Any) -> ToolCall:
    raw = tc.function.arguments
    try:
        arguments = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        arguments = {}
    # 动态保留字段：模型在 args 内返回 ``_approval_request``，harness 提升为字段并从 args 剥除
    # （真实工具永远看不到这个保留字段）。无需在工具 schema 里静态声明。
    approval_request = bool(arguments.pop("_approval_request", False))
    return ToolCall(
        id=tc.id, name=tc.function.name, arguments=arguments, approval_request=approval_request
    )


def _usage_to_dict(usage: Any) -> dict[str, int] | None:
    """OpenAI 兼容的 usage 对象 → 归一化 dict；无 usage 时返回 None。

    除基础 prompt/completion/total 外，还解析文档扩展字段：
    - ``reasoning_tokens``（推理模型思维链 token，嵌套在 completion_tokens_details）
    - ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``（上下文缓存命中情况）
    """
    if usage is None:
        return None
    d: dict[str, int] = {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }
    if (v := getattr(usage, "prompt_cache_hit_tokens", None)) is not None:
        d["prompt_cache_hit_tokens"] = int(v)
    if (v := getattr(usage, "prompt_cache_miss_tokens", None)) is not None:
        d["prompt_cache_miss_tokens"] = int(v)
    details = getattr(usage, "completion_tokens_details", None)
    if details is not None and (v := getattr(details, "reasoning_tokens", None)) is not None:
        d["reasoning_tokens"] = int(v)
    return d


# --------------------------------------------------------------------------- #
# 测试替身（provider 无关，亦实现 stream）
# --------------------------------------------------------------------------- #
class FakeModel:
    """测试替身：按预设脚本依次返回 Decision。"""

    def __init__(self, script: list[Decision]) -> None:
        self.script: list[Decision] = list(script)
        self.calls: list[list[Message]] = []
        self.tools_seen: list[list[dict] | None] = []  # 记录每次 act 收到的 tools，便于断言透传

    async def _next(self, messages: list[Message], tools: list[dict] | None = None) -> Decision:
        self.calls.append(list(messages))
        self.tools_seen.append(tools)
        if not self.script:
            return Decision(text="<script exhausted>")
        return self.script.pop(0)

    async def act(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Decision:
        return await self._next(messages, tools)

    async def stream(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> AsyncIterator[StreamEvent]:
        d = await self._next(messages, tools)
        if d.text:
            yield StreamEvent(type="text", text=d.text, kind="content")
        yield StreamEvent(type="done", decision=d)


class RecordingModel:
    """测试替身：记录 messages，可返回固定 Decision 或用回调生成。"""

    def __init__(
        self,
        decision: Decision | None = None,
        on_act: Callable[[list[Message]], Any] | None = None,
    ) -> None:
        self.calls: list[list[Message]] = []
        self.tools_seen: list[list[dict] | None] = []
        self._decision: Decision = decision or Decision(text="recorded")
        self._on_act: Callable[[list[Message]], Any] | None = on_act

    async def _next(self, messages: list[Message], tools: list[dict] | None = None) -> Decision:
        self.calls.append(list(messages))
        self.tools_seen.append(tools)
        if self._on_act is not None:
            r = self._on_act(messages)
            if hasattr(r, "__await__"):
                r = await r
            return r
        return self._decision

    async def act(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Decision:
        return await self._next(messages, tools)

    async def stream(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> AsyncIterator[StreamEvent]:
        d = await self._next(messages, tools)
        if d.text:
            yield StreamEvent(type="text", text=d.text, kind="content")
        yield StreamEvent(type="done", decision=d)


# --------------------------------------------------------------------------- #
# 生产模型：OpenAI 兼容（DeepSeek 等只是配置）
# --------------------------------------------------------------------------- #
class OpenAICompatibleModel:
    """通过 OpenAI 兼容协议调用任意 Chat Completions 服务。

    构建实例不触发网络（client 可注入），便于离线单测。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        client: Any | None = None,
    ) -> None:
        self.model: str = model
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        # client 可注入（含测试假 client），故统一按 Any 处理，避免对第三方 SDK 做重载校验。
        self._client: Any = client

    @classmethod
    def from_settings(cls, settings: Settings, client: Any | None = None) -> "OpenAICompatibleModel":
        api_key = (settings.llm.api_key or "").strip()
        if not api_key:
            raise ValueError(
                "未配置 LLM_API_KEY（模型 API 密钥）。请在项目根目录的 .env 中设置 "
                "LLM_API_KEY=sk-xxx，或导出环境变量 LLM_API_KEY；详见 CODEBUDDY.md。"
            )
        return cls(
            api_key=api_key,
            base_url=settings.llm.base_url,
            model=settings.llm.model,
            client=client,
        )

    async def act(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> Decision:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [_to_openai(m) for m in messages],
            "stream": False,
        }
        if tools:
            kwargs["tools"] = tools
        resp = await self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        tool_calls = [_from_openai(tc) for tc in (msg.tool_calls or [])]
        usage = getattr(resp, "usage", None)
        return Decision(text=msg.content, tool_calls=tool_calls, usage=_usage_to_dict(usage))

    async def stream(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> AsyncIterator[StreamEvent]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [_to_openai(m) for m in messages],
            "stream": True,
            # 请求末端补一个 usage-only chunk（DeepSeek / OpenAI 均支持）
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
        stream = await self._client.chat.completions.create(**kwargs)
        text_buf: list[str] = []
        acc: dict[int, dict[str, str]] = {}
        usage: dict[str, int] | None = None
        async for chunk in stream:
            # 任意 chunk 携带 usage 都记录。注意：DeepSeek 的 usage chunk 仍带非空
            # choices（OpenAI 规范里 usage-only chunk 才 choices 为空），故不能依赖
            # 「choices 为空」判断，否则会漏抓、使 stream 返回 usage=None。
            if getattr(chunk, "usage", None) is not None:
                usage = _usage_to_dict(chunk.usage)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            # 思考过程：DeepSeek 的 reasoning_content，及其它 provider 的 reasoning 字段
            reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if reasoning:
                yield StreamEvent(type="text", text=reasoning, kind="reasoning")
            if delta.content:
                text_buf.append(delta.content)
                yield StreamEvent(type="text", text=delta.content, kind="content")
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    slot = acc.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["arguments"] += tc.function.arguments
                    # 工具调用参数随流式逐步产出：发出增量事件，供 UI 实时预览
                    # （如 write/edit 的 content 在生成过程中即可显示，避免「大段写入时无输出」）。
                    yield StreamEvent(
                        type="tool_call_delta",
                        tc_index=tc.index,
                        tc_id=slot["id"] or None,
                        tc_name=slot["name"] or None,
                        tc_args=slot["arguments"] or None,
                    )
        tool_calls = []
        for s in acc.values():
            a = json.loads(s["arguments"] or "{}")
            # 同 _from_openai：剥除动态保留字段 _approval_request
            approval_request = bool(a.pop("_approval_request", False))
            tool_calls.append(
                ToolCall(id=s["id"], name=s["name"], arguments=a, approval_request=approval_request)
            )
        yield StreamEvent(
            type="done",
            decision=Decision(text="".join(text_buf) or None, tool_calls=tool_calls, usage=usage),
        )


def create_model(settings: Settings) -> Model:
    """工厂：按配置构建默认 provider（OpenAI 兼容）。"""
    return OpenAICompatibleModel.from_settings(settings)
