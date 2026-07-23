"""Textual 全屏 TUI 模块（M8）。

本包只负责「把事件流渲染成部件 + 收集用户输入 + HITL 模态」，不含任何 loop / 工具逻辑。
推理由 ``agent.core`` 在独立 worker 线程跑 ``Session.step`` 驱动（M8.2），
事件经 ``TextualTransport``（``agent.runtime.textual_transport``）用 ``app.call_from_thread``
安全桥接回 Textual 主线程。
"""

from __future__ import annotations

from agent.tui.app import ChatApp

__all__ = ["ChatApp"]
