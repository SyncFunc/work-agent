# 里程碑 M10 用量持久化与 message 模型

> 依据 `CODEBUDDY.MD` §7.1 判定为**双端**任务（后端 `agent/` + 前端 `desktop/` + 双端契约）。
> 本里程碑把「一次响应」提升为一等概念 `message`，让每条历史消息的 token 用量与耗时可落盘、可回放、重启可恢复。

## 目标

以 `message` 为一等概念，在后端把每轮（顶层 `task.send` + 每个子 agent 调用）的 token 用量与耗时落盘为可回放的 `USAGE` 事件；前端按 `message` 聚合（子 agent 用时向上归集到父 message、子块不渲染消耗），使桌面端重启后历史消息的用量/耗时可完整恢复。

## 前置依赖

- **M7**（agentrunner 守护进程分离）：`EventStream` / `protocol.py` / `bridge.py` 已落地。
- **M9**（Electron 桌面客户端）：前端 TS 协议库、 `useEventReducer`、流式渲染、`SubagentBlock` 已落地。
- **M5**（SubagentSpawner）：`parent_session_id` 子会话链路已支持（本次沿用）。

## 步骤索引

| 步骤 | 文件 | 目标 |
|---|---|---|
| M10.1 | [M10.1-后端事件模型与message_id打标.md](./M10.1-后端事件模型与message_id打标.md) | `events.py` / `session.py` 引入 `message_id` / `parent_message_id` + `EventType.USAGE` + 唯一打标漏斗 |
| M10.2 | [M10.2-daemon与子agent_usage事件.md](./M10.2-daemon与子agent_usage事件.md) | `server.py` / `bridge.py` / `subagent.py` 每 message emit `USAGE` 事件并计时 `duration` |
| M10.3 | [M10.3-契约三处同步.md](./M10.3-契约三处同步.md) | `protocol.py` 删 `MsgType.USAGE`，三处契约同步 + `check-msgtype` |
| M10.4 | [M10.4-前端按message聚合渲染.md](./M10.4-前端按message聚合渲染.md) | `types.ts` / `reducer` / `App` / `obs` / `toolbar` 按 message 聚合、子 agent 归集 |

## 全局约定（跨步骤铁律）

- **用量以 message 为单位**：`message` = 一次完整响应（顶层 = 一轮 `task.send`；子 agent = 一次 spawn 调用）。
- 字段：
  - `message_id`：`uuid4().hex`，每次响应生成。
  - `parent_message_id`：子 agent 指向「派生它的那条 message」（直接父）；顶层为 `null`。可嵌套，故指向**直接父 message**。
- **`duration` 语义（已定）**：后端**墙钟时间**（`step` 开始 → 返回，含 HITL ask/approve 等待），与现有前端 `Date.now()` 口径一致。
- **前端聚合规则（已定）**：子 agent 的 `usage` + `duration` 由前端按 `parent_message_id` 链向上归集到**根 message（顶层 `ResponseBlock`）** 的 `turnMeta`（tokens 累加、duration 累加）；**子 agent 块底部不渲染**任何消耗信息。
- **持久化路径**：后端一律通过 `EventStream.append(EventType.USAGE)` 落盘（复用 `SessionStoreSink`）+ 经现有 `event` 消息实时转发与回放；**不再使用** `transport.report_usage()` 的 `MsgType.USAGE` 实时消息。
- **兼容性**：`events` 表存 `ev.to_dict()` 的 JSON，新字段自动兼容，无需 migration；旧会话无 `USAGE` 事件 → 该轮前端不显示用量（优雅降级）。

## 里程碑级知识沉淀

> 本里程碑全部步骤完成后，汇总跨步骤结论（接口签名、模块边界、契约约定、踩坑）。届时同步追加到 `knowledge/INDEX.md`。
