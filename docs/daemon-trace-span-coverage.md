# Daemon Trace / Span / Log 体系调研报告

> 目标：调研 daemon 当前的可观测（trace/span/log）体系覆盖情况，对照"**一次 trace 应代表一次用户操作/交互的完整生命周期**"这一目标，找出差距并给出改进方向。
>
> 范围：后端 `agent/obs`、`agent/core`、`agent/daemon`、`agent/context`、`agent/resilience`，契约与前端 `desktop/src/features/obs`。
>
> **只调研、不实现**。所有改进项均为"建议方向"，待确认后再进入里程碑步骤。

---

## 1. 背景与目标

CODEBUDDY.md 的架构决策摘要中明确了两条全局主线：

> 事件流（状态单一事实来源）+ Trace/Span（可观测，OTel 语义，父子 parent_id）

以及：

> 可观测 | 自研 Tracer（OTel 语义），可导出 JSON / 接 Langfuse

目前可观测层的含义应当是：**一条 trace 捕捉一次完整的用户操作（用户发送一条消息 → agent 执行完成 → 返回结果）的全生命周期**，包含内部不中断的 span 树与交互等待。然而当前实现与这个目标之间存在显著差距，下文详述。

---

## 2. 当前实现现状（基于代码）

### 2.1 观测层核心数据结构

| 概念 | 位置 | 关键字段 |
|---|---|---|
| `Span` | `agent/obs/tracer.py:46` | `id/name/kind/parent_id/started_at/ended_at/meta/logs` |
| `LogEntry` | `agent/obs/tracer.py:33` | `ts/key/value/level`（level ∈ info/warn/error） |
| `Tracer` | `agent/obs/tracer.py:126` | `spans: list[Span]` + `session_id` |
| `_span()` | `agent/obs/tracer.py:104` | 统一上下文管理器；`tracer=None` 降级 no-op |
| `_SpanCtx` | `agent/obs/tracer.py:66` | `__enter__`/`__exit__` 自动 push/pop ContextVar + 设 `ended_at` |
| 隐式 parent | `tracer.py:25` | `contextvars.ContextVar[Span | None]` 传递当前活跃 span |
| `TraceStore` | `agent/obs/store.py:21` | SQLite 持久化；`save_trace()` **按 session_id 覆盖写**（`store.py:66-67`） |
| 表 `spans` | `store.py:37-48` | 主键 `(session_id, span_id)`；含 `name/kind/parent_id/起止时间/meta_json` |
| 表 `logs` | `store.py:49-57` | 主键 `(session_id, span_id, ts, key)` |

### 2.2 关键事实：当前 trace 粒度 = 整个会话

- `Session.__init__` 只创建**一个** `Tracer` 实例（`session.py:107`），并在其上挂一个 `session` 根 span（`session.py:119-126`），生命周期跨整个会话。
- 每次 `Session.step()`（标准一次用户操作的执行单位）都往**同一个** Tracer 追加一个 `agent.run` span，然后调用 `_save_trace()`（`session.py:313-314`）。
- `_save_trace()` → `TraceStore.save_trace()` 按 `session_id` **整体覆盖**（先删后插，`store.py:66-67`）。

**结论**：当前"一条 trace"= 整个 session 的全部 span 累加；并非"一次用户操作一条 trace"。
这与"一次 trace = 一次用户操作/交互的完整生命周期"**直接冲突**，是本体系最根本的粒度错配。

### 2.3 实际 span 树

```
session (kind=session, root_span, 跨多轮不关闭)           session.py:119
├── agent.run (kind=agent, 每轮 step 一个)                loop.py:194
│   ├── model.act (kind=model, 每轮迭代一次 LLM 调用)     loop.py:373
│   │   logs: conv_len, plan_mode,tool_calls,
│   │         final_text_len, decision_empty(warn), usage(meta)
│   └── tool.exec (kind=tool, 每个工具调用一个)           loop.py:475
│       logs: tool, args, unknown_tool(warn),
│              approval_ask, approval_rejected(warn), exec_error(error)
├── agent.run:<name> (子 agent, parent=root_span)         subagent.py/loop.py:194
├── context.compact (kind=compact)                        context/manager.py:208
│   ├── compact.microcompact (>0次)                       context/manager.py:170
│   └── compact.anti_drift (>0次)                         context/manager.py:276
```

### 2.4 一次用户操作（一个 `task.send`）在 daemon 中的流转

```
daemon server._task_send → (生成 message_id) → session.step(...)   server.py:309-380
  └─ AgentLoop.run(current_task, ..., parent_span=self.root_span)   loop.py:194,302
       └─ while: _decide → clarify闸门/plan闸门/最终答案闸门
            ├─ _decide: model.act (LLM流式)
            └─ _exec_tools: tool.exec (并发 asyncio.gather)
  → session.step内: 澄清回填/计划确认/最终答案                     session.py:326-366
  → self._save_trace()                                             session.py:314
```

---

## 3. 覆盖率盘点：已覆盖 / 未覆盖 / 弱覆盖

### 3.1 已覆盖（有 span + 关键 log）

| 环节 | span | 关键 log | 位置 |
|---|---|---|---|
| 会话语义根 | `session` | — | `session.py:119-126` |
| 一轮 agent 执行 | `agent.run` | clarify / stall(error) / soft_limit(warn) | `loop.py:194,209,315,345` |
| LLM 调用 | `model.act` | conv_len / plan_mode / tool_calls / final_text_len / decision_empty / usage(meta) | `loop.py:373-406` |
| 工具执行 | `tool.exec` | tool / args / unknown_tool / approval_ask / approval_rejected / exec_error | `loop.py:475-549` |
| 上下文压缩 | `context.compact` | trigger_pct / shortcut / session_memory / auto_compact | `context/manager.py:208-251` |
| 微压缩 | `compact.microcompact` | tool_results_replaced | `context/manager.py:170` |
| 防漂移 | `compact.anti_drift` | files_reread | `context/manager.py:276` |

### 3.2 未覆盖 / 弱覆盖（缺口）

| 编号 | 环节 | 现状 | 影响 |
|---|---|---|---|
| G1 | **HITL 交互（等待用户）** | `transport.ask` / `confirm_plan` / `approve` **无 span**；仅以 `agent.run.log("clarify",...)` 或 `tool.exec.log("approval_ask",...)` 记录一行 | **"完整生命周期"里最长的一段——等用户——在 trace 时间线上不可见**；无法区分"AI 在思考"和"在等用户"。 |
| G2 | **计划呈现 `present_plan` / `show_plan`** | 无 span（`session.py:340` 只发事件，无 timing 记录） | 计划呈现→用户审阅→确认这段耗时无法观测。 |
| G3 | **斜杠命令 `/command`（/exec / /approve / /context 等）** | `dispatch_command` 在 `session.step` **之外**执行（`server.py:487-490`），完全不进 trace | 一类明确的"用户操作"完全无观测。 |
| G4 | **后台子 Agent / 会话记忆抽取** | `spawn_background` 起的子 `agent.run:<name>` 塞进**同一个 session tracer**（`session.py:107`），但时序上与前台 step 交错，无归属到某次用户操作的标记，且无 `spawn_background` 自身的包裹 span | 后台任务耗时与前台混入同一 trace，难以按用户操作归因。 |
| G5 | **韧性层 Pipeline（限流/熔断/降级/重试）** | `build_llm_pipeline` / `build_sandbox_pipeline` 包裹了执行入口，但 Pipeline 各步骤 **零 tracing**（`resilience/pipeline.py` 无任何 span） | "为什么慢 / 为什么被阻断"的**最关键决策层完全透明缺失**：限流拒绝、熔断打开、降级、重试次数都看不到。 |
| G6 | **沙箱执行** | 只有 `tool.exec` span 包裹整个 `_run_bash_in_sandbox` 调用，无独立 span 区分 | 工具逻辑耗时 vs 沙箱开销不可分离。 |
| G7 | **模型流式/用量/重试** | 仅 `model.act.meta["usage"]` 记录了 total_token 聚合值，无 `finish_reason`、无 prompt/completion 分项、无限流重试记录 | Token 成本优化与失败原因不可追溯。 |
| G8 | **错误路径 trace 丢失** | `loop.run` 抛 `LoopStalled`（`loop.py:320`）或 `step` 抛异常时，`_save_trace()`（`session.py:314`）**不会被执行**；daemon `_run` 的 catch-all（`server.py:369-370`）仅 `transport.notify`，不保存 trace | 失败/中断/出错的运行**没有 trace 可查**——而失败恰恰是最需要可观测的时刻。 |
| G9 | **span 错误态未标记** | `model.act` / `tool.exec` 在异常退出时因 `_SpanCtx.__exit__` 标记了 `ended_at` 但 status 仍是 "ok"；`LoopStalled` 靠异常从 `with _span` 逃逸才标记 error | 一眼看不出哪次 span 失败了；需要遍历 logs 找 error record。 |
| G10 | **双主线未打通** | EventStream 是"状态单一事实来源"，但 `Event` **不带 `span_id`/`trace_id`**（`core/events.py`）；trace 与事件 timeline 无法对照 | CODEBUDDY 所述"事件流 + Trace/Span 双主线"目前是**两条平行互不关联的线**。 |
| G11 | **Langfuse/OTel 导出未实现** | 仅 `tracer.py:161` 有注释 "供导出 Langfuse 等"，实际 **无任何导出代码**（`grep langfuse/opentelemetry → 0 hits in agent/`） | 技术债：架构声明与实现不一致，无法接入外部可观测平台。 |
| G12 | **没有跨请求/跨组件 Trace 关联** | 所有 span 都在同一个进程/同一 Tracer 内；无 `traceparent`/`trace_id` 写入 HTTP 或日志，无法把 daemon 内部 trace 与前端请求/外部监控关联 | 端到端可观测性缺环。 |
| G13 | **日志体系与 span 断层** | 当前用 `span.log()` 手动调用记录日志（`loop.py:209,315,345,375,400,404,477-478,529,533,549`、`context/manager.py:175,209,216,228,243,278`），既不经过 Python 标准 `logging` 模块，也不与 `logging.getLogger()` 的调用穿透。开发者必须在两套 API 间抉择：业务日志走 `logging`，观测日志走 `span.log`，易遗漏、不一致 | ① 日志分散在 span.log + logging 两套体系，无法统一过滤/归档；② 每个新 span 需开发者记得手动调 `span.log`，引入遗漏窗口；③ 外部日志采集（文件/ELK/Datadog）无法感知 span 边界，丢失上下文关联。 |

---

## 4. 根因分析（架构级问题）

1. **粒度错配（最根本）**：Tracer 是 session 级单例（`session.py:107`），`save_trace` 按 session 覆盖写（`store.py:66-67`），导致一条 trace = 一整个会话。这与"一次 trace = 一次用户操作"的目标在起点就相反。
2. **trace 无身份标识**：trace 不带 `message_id` / 用户输入文本 / 操作类型标签，无法按"哪次用户操作"检索与命名。前端 `TraceTree` 只能按 `session_id` 列出（`desktop/src/features/obs/TraceTree.tsx:39`），列表里全展示为 `session_id` 片段，毫无辨识度。
3. **持久化主键是 session_id**：`spans` 表主键 `(session_id, span_id)`（`store.py:47`），无法在同一 session 下并列保存多次操作的独立 trace；`list_traces` 一个 session 只返回 0 或 1 条（`store.py:155-183`）。
4. **协议与实现强耦合 `trace_id == session_id`**：`protocol.py:20-24` 注释明确 "trace_id == session_id"，`_trace_get` 按 `trace_id`（实为 session_id）查询（`server.py:536-559`）。要支持 per-op trace，**存储、协议、前端三处都要联动修改**（符合 7.1 双端契约原则）。
5. **"OTel 语义"未落地**：仅在 `obs/__init__.py:1` 的 docstring 声明。实际：
   - `kind` 是自由字符串，无受控枚举，不符合 OTel SpanKind（`INTERNAL`/`CLIENT`/`SERVER`/`PRODUCER`/`CONSUMER`）；
   - `LogEntry.level` 是自由字符串，无标准 severity（trace/debug/info/warn/error/fatal）；
   - `Span` 无 status 枚举（`StatusCode.OK`/`StatusCode.ERROR`），仅以 `ended_at` 是否为 None 隐含 "open"；
   - **无任何 OTel 或 Langfuse 真实导出**。
6. **HITL 没有 span 抽象**：等待用户是"完整生命周期"的核心部分，但当前仅用一行 log 记录「发生了交互」，未把"等待"本身建模为带时长/状态/子事件的 span。
7. **并发 parenthood 风险**：隐式 parent 依赖 `contextvars`（`tracer.py:25`）。前台并行工具（`asyncio.gather`，`loop.py:4-5`）靠 within `agent.run` 上下文捕获尚可；但后台子 Agent、会话记忆子 Agent 在 `agent.run` 上下文退出后运行，其 `agent.run` parent 退化为 `root_span`（`session.py:417`）。一旦引入 `user.op` 根 span，若忘记显式 parent 传递就会串台。
8. **日志与 span 两套 API 并行，无自动归属**：开发者需要用 `span.log(key, value, level)` 手动打观测日志（`loop.py:375,404,477,529` 等 20+ 处），同时再用 `logging.info/debug/error` 输出程序日志。两套系统之间无关联——`logging` 不知道当前 span，`span.log` 不经过标准日志管道。一个工具的错误可能有 `tool_span.log("exec_error", msg, level="error")` 和 `log.error("tool exec failed: %s", e)` 两条路径，归档时可能只看漏一条。往深了说，这导致了：
   - 所有新代码都要做"记录 span.log 了吗"的心智负担；
   - 无法利用已有 `logging` 生态（filter/formatter/handler/第三方集成）；
   - 第三方库的日志不可能自动关联到 span。**解决方向是提供一个 logging Handler，通过 `contextvars` 感知当前 span 并自动写入 span.logs，最终消除 `span.log()` 显式调用。**

---

## 5. 改进方向建议（非实现，待确认）

### 5.1 重新定义 trace 粒度

- 把**一次用户操作**（= `session.step` 的一次完整执行，含其内部的澄清回填循环 / 计划确认循环）作为**一条 trace**，`trace_id` 与 `message_id` 绑定。
- `save_trace` 改为**追加按 `trace_id` upsert**，主键变为 `(session_id, trace_id, span_id)`；不再按 session 整体覆盖。
- 保留可选的"会话级聚合视图"（按 session_id 汇总多条 `user.op` trace），但不作为唯一存储单位。

### 5.2 引入 `user.op` 根 span

一次用户操作的 span 树建议为：

```
user.op (kind=interaction, 携带 message_id / 用户输入文本 / 操作类型)
├── agent.run                          # 模型自主执行段
│   ├── model.act                      # 一轮 LLM 调用（含 finish_reason / token 分项）
│   └── tool.exec                      # 每个工具调用
│       ├── tool.sandbox (可选)        # 沙箱执行子 span
│       └── tool.gate (可选)           # 审批门子 span
├── plan_present (kind=interaction, 有 waiting 属性)
│   └── plan_confirm (kind=interaction, waiting)
├── clarify (kind=interaction, waiting)
│   └── [每个 clarification 往返]
├── tool_approve (kind=interaction, waiting)  # gate.authorize
├── subagent.spawn (kind=subprocess)          # 委派子 Agent（新 trace 或子 trace）
└── [后台任务] session_memory.extract         # 记忆抽取子 Agent
```

要点：
- **HITL 交互全部建模为独立 span**，标注 `status=waiting`（用时 = 真实等待用户时长），把"等用户"纳入 trace。
- `subagent.spawn` 既可以在当前 trace 内作为子 span，也可作独立 trace 通过 `traceparent` 关联——取决于后端子 Agent 是否同进程。

### 5.3 HITL 交互建模（补 G1/G2）

- 新增 `plan_present` / `clarify` / `tool_approve` 三类 span（kind 建议 `interaction`）。
- span 在 `await transport.ask/confirm_plan/authorize` **之前**创建，`await` 返回后结束；时长 = 真实等待用户耗时。
- 在 span 的 log 中记录 `question`/`plan`/`action` 内容摘要，便于回溯"用户在等什么"。
- `/command` 视情况纳入：明确"用户操作"是否包括命令分发（补 G3）。建议按"有用户输入的都算"把 `/command` 也纳入 `user.op` 级别。

### 5.4 韧性层统一包裹 span（补 G5/G6）

- `Pipeline.execute` / `Pipeline.execute_stream` 前包裹 span（kind 建议 `resilience`），记录：
  - `decision`: `rate_limited` / `circuit_open` / `fallback` / `retry` / `direct`
  - `retry_count`, `total_wait_ms`, `downstream=llm|sandbox`
- sandbox 执行拆出独立 `tool.sandbox` span（`tool.exec` 之下），区分工具逻辑耗时与沙箱开销（补 G6）。
- 这需要在 `build_llm_pipeline` / `build_sandbox_pipeline` 中注入 `tracer`。

### 5.5 错误路径与错误态（补 G8/G9）

- `session.step` / daemon `_run` 用 `try/finally` 保证**无论成功失败都保存 trace**。
- `Span` 增加 `status: str` 枚举（`ok`/`error`）；异常退出路径标记 error 并写入 `error` meta（异常类型/消息/stack trace 可选）。
- 软上限 `soft_limit` 建议标记为 warn 状态（如果引入）而非 error。

### 5.6 打通双主线（补 G10）

- `Event` 增加可选字段 `trace_id` / `span_id`（或 `op_id` = user.op 的 trace_id）。
- 前端 timeline 模式可把事件视图对齐到产生它的 span，实现"事件 ↔ span"对照。
- 这需要权衡：每 append 一个 Event 时，如果 Tracer 中有当前 span 则自动写入。

### 5.7 收敛到 OTel 语义（补 G11）

- `Span.kind` 改为受控枚举或至少规范化为 OTel 标准值：`INTERNAL` / `CLIENT` / `SERVER` / `PRODUCER` / `CONSUMER`，当前 `model`/`tool`/`agent`/`compact` 等映射到 `INTERNAL`，`interaction` 映射到 `CLIENT`。
- `LogEntry.level` 收敛为标准 severity number（如 Python logging 级别或 OTel SeverityNumber）。
- `Span` 增加 `status_code` / `status_message` 字段。
- 设计统一的 `to_otel()` / `to_langfuse()` 导出方法，保持对外格式稳定。

### 5.8 持久化升级

- 表 `spans` 主键改为 `(session_id, trace_id, span_id)`；新增 `trace_id` 列（关联 message_id）和 `message_id` 索引。
- `list_traces` 升级为同时支持按 session 聚合和按 trace 枚举。`session_id` 列从主键降级为分区键。
- 向前兼容：历史数据中 trace_id 可以用 session_id 回填。

### 5.9 双端契约影响（三处联动，符合 7.1）

| 现协议字段 | 建议变更 | 影响方 |
|---|---|---|
| `trace.list` payload `session_id?` | 不变（支持按 session 过滤） | server / protocol / types.ts |
| `trace_list` 返回 `traces[]` 中每项 | 增加 `trace_id`、`message_id`、`user_text`、`status` 字段；现有 `session_id` 改为对应会话的 session_id（而非 trace 的身份） | protocol / types.ts / TraceTree.tsx |
| `trace.get` payload `trace_id` | 同上，trace_id 改为 message_id 而非 session_id | protocol / types.ts / TraceTree.tsx |
| `trace_tree` 返回 | 新增 `trace_id` / `session_id` 顶层字段 | protocol / types.ts |
| `SpanNode` 结构 | 增加 `status_code` / `status_message` / `attributes` 字段 | types.ts / TraceTree.tsx |

- **契约一致性**：变更后仍需跑通 `scripts/check-msgtype.mjs` 保证 `MsgType` 与 `ALL_MSG_TYPES` 同步；`docs/daemon-api.md` 同步更新 5.17/5.18 及 6.8 节 payload 描述。

### 5.10 请求入口生成 trace_id，串联整体链路（补 G12）

**问题**：当前 trace_id 仅在 `Session.__init__` 时（`session.py:107`）生成为 `uuid4()`，与外部请求完全脱钩。同一 session 内的多轮次 step 共享同一 trace_id，且无身份信息。

**建议方向**：

1. **trace_id 生成时机提前到请求入口**：在 daemon `_route` 或 `_task_send`（`server.py:309`）收到 `task.send` 消息时即生成 `trace_id`（建议 `tracestate` 格式或消息级 uuid，非 session 级），与 `message_id` 绑定。此时 span 尚未创建，trace_id 作为元数据传入 `Session.step(message_id=..., trace_id=...)`。
2. **前端也可生成并传递 trace_id**：请求入口包括 WebSocket 消息收发的 daemon 侧和浏览器/Electron 侧。前端可在 `sendMessage` 时生成一个 `trace_id` 放入 payload，后端子 Agent 处理时用此 trace_id 创建 Tracer，实现端到端串联。协议中增加可选的 `trace_id` 字段（`task.send` + `task.send_background`）。
3. **trace_id 跨组件传播**：trace_id 写入 daemon 的日志记录器（Python logging 的 extra），后台子 Agent 的 spawn 也继承父 trace_id。最终输出到 `trace_store` 的 `trace_id` 列。
4. **trace_id 有身份**：trace 在持久化时携带 `message_id`、用户输入摘要（首 50 字符）、`operation_type`（task/command），使前端 `trace.list` 列表可辨识"哪次操作"。

### 5.11 标准 logging 自动归属 span（补 G13，替代 `span.log`）

**问题**：当前强制使用 `span.log(key, value, level)` 手动记录观测日志，开发者必须在两套 API（`span.log` vs `logging`）间切换，新代码极易遗漏。

**建议方向**：

实现一个 **Python logging Handler**，通过 `contextvars` 感知当前活跃 span（`_current_span: ContextVar[Span]`，已存在 `tracer.py:25`），自动将 log record 写入 span：

```python
class SpanLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        span = _current_span.get(None)
        if span is not None and record.levelno >= logging.DEBUG:
            span.log(
                key=record.name,  # logger name
                value={
                    "msg": record.getMessage(),
                    "level": record.levelname,
                    "module": record.module,
                    "line": record.lineno,
                    # 其余 extra 自动带入
                    **(record.__dict__.get("extra") or {}),
                },
                level=record.levelname.lower(),
            )
```

效果：
- 现有代码中 `log.info("tool %s completed", tool_name)` 自动归属到当前 span，**无需任何 `span.log` 显式调用**。
- 第三方库的日志（如 `httpx`、`aiosqlite`）在 span 上下文内输出时，也自动进入 span，大幅提升可观测粒度。
- 开发者只需理解 Python 标准 `logging` 模块，无需学习 `span.log` API，降低接入门槛。
- **最终可以淘汰 `span.log()` 显式调用**：将所有现有 `span.log()` 替换为等价的 `logger.info(...)`，保留 `span.log()` 方法但不推荐新代码使用。

注意事项：
- log level 映射：`logging.ERROR` → `span.log(level="error")`，`logging.WARNING` → `"warn"`，`logging.INFO` → `"info"`，`logging.DEBUG` → `"debug"`。注意 `warn`（Python logging 标准）与 `"warn"`（当前 span.log level 名称）的兼容。
- 性能：Handler 只做 dict 写入和 `_current_span.get()`（O(1)），开销极小；`addHandler` 加在 root logger 上或设置一个 `trace_logger` 专用 logger。
- 调试/非 trace 环境：无 span 时 Handler 静默 no-op，不影响普通 logging 行为。

### 5.12 消除 `span.log()` 显式调用，统一日志体系

在 5.11 的基础上，分阶段推进：

1. **Phase A（Handler 就位，并行）**：注册 `SpanLogHandler`，现有 `span.log()` 与新 logging 并行，确保不破坏已有日志。
2. **Phase B（存量迁移）**：将 `agent/core/loop.py`、`agent/context/manager.py`、`agent/subagent.py` 中所有 20+ 处 `span.log(...)` 依次替换为 `log.info(...)` / `log.warning(...)` / `log.error(...)`。逐处验证：
   `loop.py:209`: `root_span.log("clarify", ..., level="warn")` → `log.warning("clarify: %s, need_ask=%s", text, need_ask)`
   `loop.py:315`: `run_span.log("LoopStalled", ..., level="error")` → `log.error("LoopStalled: %s", reason)`
   `loop.py:375,404,477-478,529,533,549` 类推。
3. **Phase C（冻结 API）**：标记 `Span.log()` 为 deprecated（可选类型检查告警），未来版本移除。所有观测日志统一走 `logging` + `SpanLogHandler` 通道。
4. **Phase D（补齐）**：在缺口最大的 HITL 区域（`transport.ask`/`confirm_plan`/`approve` 前后）添加对应的 `log.info` 调用，自动写入 HITL span 的 log。

---

## 6. 开放问题（需确认后推进）

1. **trace 粒度范围**：是否"一次 trace = 一次 `task.send` 对话交互"，还是把 `/command` 也纳入？如果是后者，`/context`、`/exec`、`/bg` 等一步到位命令与多步 `task.send` 的 span 树结构不同，需要统一抽象。**建议**：按"有用户输入的都算"一刀切纳入。

2. **后台子 Agent 归入**：`spawn_background` 启动的子 Agent（会话记忆、/bg 手动启动）是否各有独立 trace，还是挂到触发它的 `user.op` 的子 span 下？**建议**：作为独立 trace 通过 `trace_id` 关联回父 trace，避免前台 trace 被后台任务污染。

3. **历史数据迁移**：当前已有持久化的 trace（按 session_id 覆盖写）怎么办？**建议**：`save_trace` 升级后，读旧数据时用 session_id 回填 trace_id 字段，写新数据时渐进提升。

4. **是否保留 session 级汇总 trace**：如果需要"一眼看全会话跨度"视图，保留一个轻量 session 根 span（仅元数据），不存子 span，子 span 各自属于各自的 `user.op` trace。

5. **并发 parenthood 的确定策略**：隐式 contextvars 与显式 parent 各有利弊。当前混合使用（`loop.py:194` 显式传 `parent_span`，`tool.exec` 用隐式 ContextVar）已基本安全。`user.op` 引入后，建议**全程显式 parent 传递**，消除隐式依赖（尤其当后台任务与前台并发时）。

6. **log 迁移兼容策略**：`SpanLogHandler` 上线后，`span.log()` 与 `logging` 会并行一段时间。何时标记 deprecated、何时删除 `span.log()` 方法？迁移期间如何确保存量 `span.log()` 不丢数据？**建议**：`span.log()` 内部改为先 emit 到 `logging.getLogger("agent.trace").handler`，再写回 span，保证双向同步，避免过渡期断档。

---

## 7. 本文件的知识沉淀

- **关键发现**：当前 trace 粒度 = 会话（session）级，不是用户操作级。`Tracer` 跨 step 单例 + `save_trace` 覆盖写是根因。
- **最大的 span 覆盖率缺口**：HITL（等待用户）和韧性层（限流/熔断/降级/重试）完全没有 span，而这二者对理解"时间去哪了"至关重要。
- **双主线脱节**：EventStream 与 Trace/Span 之间无关联 ID，无法在 timeline 上对照。
- **修复路径"先改契约，后改实现"**：`protocol.py` + `docs/daemon-api.md` + `types.ts` 三处同步（见 5.9），以 MsgType 为单一事实来源。
- **trace_id 应生成在请求入口**（`_task_send`/前端 `sendMessage`），与 `message_id` 绑定，串联端到端链路。非 session 级 uuid。
- **日志体系应通过 logging Handler 自动归属到 span**，利用已有 `_current_span` ContextVar 自动注入，最终淘汰 `span.log()` 显式调用，消除两套 API 并行的心智负担。

---

*本文基于 `agent/obs/tracer.py`、`agent/obs/store.py`、`agent/core/session.py:107-126,278-373`、`agent/core/loop.py:194-549`、`agent/context/manager.py`、`agent/daemon/server.py:309-380`、`agent/daemon/bridge.py`、`desktop/src/features/obs/TraceTree.tsx` 等代码调研完成。*
