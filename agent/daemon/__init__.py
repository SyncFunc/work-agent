"""M7：agentrunner 守护进程分离（daemon 包）。

本包实现「常驻守护进程 + 前端」架构：
- ``protocol``：WebSocket 消息信封与编解码（client / server 共用）。
- ``registry``：多 ``Session`` 索引 + 每会话环形缓冲 + 每会话锁。
- ``bridge``：服务端 ``BridgeTransport(AgentTransport)``，事件转发 + HITL future 闭环。
- ``server``：WebSocket 服务 + 本地 HTTP /health。
- ``client``：CLI 客户端，复用 ``TerminalTransport`` 渲染 + HITL 回传。
- ``session_command``：从 cli 抽取的命令分发（进程内与 daemon 共用，单一来源）。

core（loop / session / transport / events）保持零 / 极小改动。
"""

from agent.daemon.protocol import DAEMON_VERSION, PROTOCOL_VERSION

__all__ = ["DAEMON_VERSION", "PROTOCOL_VERSION"]
