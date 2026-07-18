# 重构：统一传输层（AgentTransport）+ 事件线格式 + ToolRisk 枚举

> 触发：与 M2 设计文档比对，评估 M1 当前代码是否有「UI/交互耦合」「同一概念多套表示」问题，并判断网页版可行性。
> 范围：**纯架构对齐重构，不落地 M2 的 sandbox/approval gate**。
> 结论先行：M1 架构是对的（协议驱动、core 不依赖 typer/rich），无需大改；仅做两处轻量预防性对齐 + 消除 bash/fs 的轻微冗余。

## 一、评估结论（为什么重构）

1. **双 UI 协议耦合别扭**：原 `SessionUI`（请求/响应型 HITL）与 `LoopPresenter`（推送型流式渲染）是两套协议，网页客户端要同时实现两个接口；且 `LoopPresenter` 部分方法靠 `getattr` 容错（接口漏风）。→ 合并为单一 `AgentTransport`。
2. **无实时事件线格式**：`EventStream` 只在 `run` 结束时随 `AgentResult` 返回，执行期唯一实时通道是终端特化的 `LoopPresenter` 回调，无法直接喂 websocket。→ 确立 `EventStream` 为唯一实时线格式（订阅驱动）。
3. **bash / fs 拆分评估**：模块拆分**不冗余**（shell 执行 vs 文件系统操作正交）；真正冗余在 `fs.py` 内部（`read/grep/edit` 重复「resolve→is_file→read_text→split_lines」）。→ 抽 `_load_file` 助手。
4. **risk 弱类型**：`RISK_LEVELS` 是字符串元组，靠 `order.index` 比较，易拼错。→ 提升为 `ToolRisk` 枚举（类型安全、为 M2 审批门铺路）。

## 二、实现方案

### 2.1 EventStream 实时订阅（`agent/core/events.py`）
- 新增 `subscribe(sink)` / `unsubscribe(sink)`；`append` 在写入 `seq`/`ts` 后**同步**把事件分发给所有订阅者（O(1) 写 + O(subscribers) 分发）。
- 新增 `emit(ev)`：**只分发、不入档**（用于瞬时 `tool_call_delta` 预览），不影响 `to_json`/`from_json` 与「持久化事件序列」不变量。
- `Event` 增加 `tc_index/tc_name/tc_args` 字段（承载 `tool_call_delta` 增量）。

### 2.2 单一 AgentTransport（`agent/core/transport.py`，新建；删除 `presenter.py`）
合并原 `SessionUI` + `LoopPresenter` 为 `AgentTransport` 协议：
- HITL：`interactive` / `ask` / `show_questions` / `show_plan` / `confirm_plan` / `notify`
- 渲染：`bind(stream: EventStream) -> None`（订阅事件自行渲染）
- 收尾：`close()` / `report_usage(usage, answer=None)`

`loop.run(task, ..., transport=None)` 在创建 `EventStream` 后调用 `transport.bind(stream)`；渲染完全由订阅方在 sink 内处理，loop **不再有 `presenter` 参数、不再 `getattr` 容错**。

### 2.3 loop / session 接线改造（`agent/core/loop.py`、`agent/core/session.py`）
- `loop.run` / `_decide` / `_exec_tools`：删除 `presenter` 参数与所有 `getattr(presenter, ...)` 调用；文本落 `text` 事件、`tool_call_delta` 走 `emit`、工具调用/结果/计划进度仅落事件（订阅方渲染）。业务逻辑（并发 `Semaphore`、`_risk_blocked`、澄清/计划闸门、`soft_limit_hit`）**逐字保留**。
- `Session.step(task, transport, *, yes, fatal_plan_decline)`：合并原 `ui` + `presenter` 为单一 `transport`；内部 `ui.*` 调用改为 `transport.*`。

### 2.4 CLI 统一实现（`agent/cli.py`）
- 删除 `_RichPresenter` + `_TyperUI`；新增 `TerminalTransport(AgentTransport)`：HITL 方法（`ask`/`show_plan`/`confirm_plan`/`notify` 等）与事件渲染（rich Live/Panel）合并进同一类。
- `_on_event(ev)` sink 映射：`text→on_text`、`tool_use→on_tool_call`（并把 `tc` 按 `id` 记入 `_tc_by_id`）、`tool_call_delta→on_tool_call_delta`、`tool_result→on_tool_result`（从 `_tc_by_id` 取工具名）、`plan_progress→on_plan_progress`（增量更新本地 `_plan_steps` 后渲染）、`decision→_on_decision_done`（收尾流式段/工具预览 Live）。`plan`/`clarify`/`final` 由 HITL 或已流式文本覆盖，sink 忽略。
- `run` / `chat`：`session.step(task, transport)` 不再传 `presenter`；`step` 后 `transport.close()` + `transport.report_usage(res.usage, res.text)`。

### 2.5 ToolRisk 枚举 + fs 去重（`agent/runtime/registry.py`、`agent/tools/fs.py`、`agent/tools/bash.py`）
- `registry.py`：新增 `class ToolRisk(str, Enum): READ/EDIT/EXEC`；`RISK_LEVELS = tuple(r.value for r in ToolRisk)`；`ToolSpec.risk: ToolRisk`、`tool(risk: ToolRisk=...)`。`register` 校验与 loop 风险门控仍用 `RISK_LEVELS` 比较（str 枚举兼容）。
- `fs.py`：新增 `_load_file(path) -> (Path, str)`（resolve→is_file→read_text），供 `read`/`grep`/`edit` 复用；`edit` 保留 `target` 用于写回；`risk=ToolRisk.READ/EDIT`。`write` 的「不存在即空」语义单独保留。
- `bash.py`：`risk=ToolRisk.EXEC`。

## 三、验收标准

- [x] `pytest -q` 全绿（85 passed）：含 basic flow 事件序列、并发、stall、soft-limit、未知工具、事件往返、usage 累加、PLAN 门控/进度、bash 只读白名单、CLI run/plan/clarify/trace、fs 读/写/改/grep、delta 预览、chat 模式切换。
- [x] `agent/core/presenter.py` 已删除；全局不再有 `LoopPresenter`/`SessionUI` 代码引用（仅文档/注释提及历史）。
- [x] `loop.run` / `session.step` 签名无 `presenter`，渲染完全由 `EventStream` 订阅驱动（无 `getattr` 容错）。
- [x] `EventStream.subscribe/emit` 可用；持久化 `to_json`/`from_json` 序列结构不变（瞬时 `tool_call_delta` 不入档）。
- [x] 网页版路径验证（设计层面）：再实现 `WebTransport(AgentTransport)`，在 `bind` 里 `stream.subscribe(lambda ev: ws.send(ev.to_dict()))` 即可转发，无需改 loop/session。
- [x] `ToolRisk` 枚举生效；工具用 `risk=ToolRisk.*`；M2 审批门可直接消费 `ToolSpec.risk`。
- [x] `fs.py` 的 `read/grep/edit` 经 `_load_file` 去重；`edit` 写回正常（无 `NameError`）。

## 四、知识沉淀（已追加至 `knowledge/INDEX.md`）

- 双协议合并为 `AgentTransport`；`EventStream` 为唯一实时线格式（`subscribe`/`emit`）；`ToolRisk` 枚举；`fs._load_file` 去重；bash/fs 拆分不冗余；回归保障（测试改用事件订阅式假 transport）。**铁律：不要给 loop 重新加 `presenter` 回调参数**——新增实时渲染请走事件。
- 对 M2 约束：M2 的审批 HITL 回调（原 M2.5 文档称 `SessionUI.approve`）应加在统一协议 `AgentTransport`（而非新建第三协议）；gate 经窄协议 `ApprovalUI` 消费（`AgentTransport` 结构满足）；审批/沙箱门控接入 `loop` 时直接读 `ToolSpec.risk`/事件，不依赖任何 presenter。
