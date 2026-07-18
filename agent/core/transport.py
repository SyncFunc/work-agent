"""统一传输协议（AgentTransport）。

把「人机交互（HITL）」与「事件渲染」收敛到单一契约，替换原先分裂的
``SessionUI``（请求/响应型 HITL）与 ``LoopPresenter``（推送型流式渲染）两套协议。

设计要点：
- HITL：``interactive`` / ``ask`` / ``show_questions`` / ``show_plan`` / ``confirm_plan`` / ``notify``
- 实时渲染：``bind(stream)`` 订阅 ``EventStream``，执行期事件由订阅驱动
  （终端 rich 渲染 / 未来 web 把 ``Event.to_dict()`` 序列化发 websocket）。
- 收尾：``close()``；用量汇报：``report_usage()``。

core 层（loop / session）只认本协议，不依赖 typer / rich；未来做网页版时只需再实现
一套 ``AgentTransport``（订阅事件转发 websocket）即可，**无需改动 loop / session**。

渲染转移说明：原先 loop 通过 ``LoopPresenter`` 的 ``on_text`` / ``on_tool_call`` /
``on_tool_result`` / ``on_plan_progress`` / ``on_decision_done`` / ``on_tool_call_delta``
等回调推送渲染；重构后 loop 只往 ``EventStream`` 落事件（含瞬时 ``tool_call_delta``），
渲染完全由订阅方在 ``bind`` 时注册的 sink 处理。这样消除「同一概念两套接口 + hasattr
容错漏风」，并确立 ``EventStream`` 为唯一实时线格式。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from agent.core.events import EventStream
    from agent.core.intent import Question
    from agent.core.loop import AgentResult


@runtime_checkable
class AgentTransport(Protocol):
    """Agent 对外唯一交互契约：人机交互 + 事件订阅渲染。"""

    @property
    def interactive(self) -> bool:
        """当前是否处于可交互环境（可向用户提问/确认）。"""
        ...

    async def ask(self, question: "Question") -> str:
        """向用户提出一条澄清问题并返回其答案（仅交互环境调用）。"""
        ...

    def show_questions(self, questions: list["Question"]) -> None:
        """非交互环境下，展示无法回填的澄清问题（随后会以 err_code=2 退出）。"""
        ...

    def show_plan(self, res: "AgentResult") -> None:
        """展示模型产出的计划（正文 + 步骤 + 计划文件路径）。"""
        ...

    async def confirm_plan(self) -> bool:
        """询问用户是否执行当前计划（仅交互环境调用；异步，因在事件循环内被 await）。"""
        ...

    def notify(self, message: str) -> None:
        """输出一条提示/状态信息（非最终答案，如模式切换、计划未确认等）。"""
        ...

    def bind(self, stream: "EventStream") -> None:
        """订阅 ``EventStream`` 以渲染/转发事件（终端 rich / 未来 web 序列化）。

        loop 在 ``run`` 内创建 ``EventStream`` 后调用本方法；传输方自行注册订阅器，
        执行期事件即实时到达，无需等待 ``run`` 结束。默认无操作（无 UI 场景）。
        """
        ...

    def close(self) -> None:
        """一轮 ReAct 循环结束：清理渲染状态（如停止 Live）。"""
        ...

    def report_usage(self, usage: dict[str, int] | None, answer: str | None = None) -> None:
        """打印 token 用量（usage 为空时用 answer 粗略估算）。"""
        ...
