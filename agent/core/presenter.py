"""流式渲染协议（LoopPresenter）。

把「ReAct 循环内部的实时事件」与具体渲染实现解耦：core 层（loop）只按协议
回调，不去依赖 rich / typer 等具体 UI 框架；CLI 层用 rich 实现本协议，测试
可注入假实现驱动分支、无需真实终端。

事件分类（对应需求「区分思考 / 输出 / 工具调用」）：
- ``on_text(text, kind)``：流式文本。``kind="reasoning"`` 是模型思考过程，
  ``kind="content"`` 是正式输出。
- ``on_tool_call(tc)``：模型发起工具调用。
- ``on_tool_result(tc, res)``：工具执行结果回执。
- ``close()``：一轮 ReAct 循环结束，渲染器清理（如停止 Live 视图）。

所有方法均可选实现（loop 用 hasattr 容错），未实现即被静默跳过。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from agent.core.model import ToolCall
    from agent.runtime.registry import ToolResult


@runtime_checkable
class LoopPresenter(Protocol):
    """ReAct 循环实时渲染协议（core 层不依赖具体 UI 框架）。"""

    def on_text(self, text: str, kind: str) -> None:
        """流式文本。``kind`` ∈ {"reasoning", "content"}。"""
        ...

    def on_tool_call(self, tc: "ToolCall") -> None:
        """模型发起一次工具调用。"""
        ...

    def on_tool_result(self, tc: "ToolCall", res: "ToolResult") -> None:
        """工具执行结果回执（含 ok / output / error）。"""
        ...

    def close(self) -> None:
        """一轮 ReAct 循环结束：清理渲染状态（如停止 Live）。"""
        ...
