# Daemon 接口文档（WebSocket 协议）

> **用途**：本文档是前端（Electron 客户端）与后端（Python `agent/daemon`）之间 WebSocket 通信接口的**权威参考**。
> - **后端（daemon）同学**：接口有改动（新增消息类型 / 修改 payload 字段 / 行为变化）时，必须同步更新本文件、`agent/daemon/protocol.py`、以及 `desktop/src/protocol/types.ts`（并跑通契约测试）。
> - **前端（客户端）同学**：凡涉及与后端交互的需求，先查本文档确认消息类型与 payload，再对齐 `desktop/src/protocol/types.ts`。
>
> **单一事实来源（MsgType 名称）**：`agent/daemon/protocol.py` 的 `MsgType` 枚举。`desktop/src/protocol/types.ts` 的 `ALL_MSG_TYPES` 通过契约测试 `scripts/check-msgtype.mjs`（由 `tests/unit/test_m9_protocol_contract.py` 调用）保证与之一致。
>
> **payload 与行为事实来源**：以**当前运行的实现**为准——路由在 `agent/daemon/server.py`（`_route` / `_attach` / `_task_send` 等），S2C 事件与 HITL 请求在 `agent/daemon/bridge.py`（`BridgeTransport`）。本文档据此编写。
>
> ⚠️ **已知漂移**：`protocol.py` 中部分 docstring 与真实实现不一致（见文末「待对齐项」）。本文档以 `server.py` / `bridge.py` 的真实行为为准。

---

## 1. 传输与连接

| 项 | 值 |
|---|---|
| 传输层 | WebSocket（仅绑定回环地址 `127.0.0.1`，由 `settings.daemon.host/port` 配置） |
| 子协议（Sec-WebSocket-Protocol） | `agent-daemon-{PROTOCOL_VERSION}`，当前 `PROTOCOL_VERSION = "3.1"` |
| 数据格式 | 每条消息为一段 JSON 文本（`json.loads` 解析） |
| 健康检查 | 独立 HTTP：`http://127.0.0.1:{health_port}/health`，返回 `{"status":"ok","daemon_version":"1.0.0"}` |
| 鉴权 | 可选。若 `settings.daemon.token` 非空，`hello` 必须带 `token`，否则服务端回 `error`（`code:"auth"`） |

---

## 2. 消息信封（Envelope）

所有消息共用同一信封结构（由 `protocol.make_message` 构造、`protocol.parse_message` 解析）：

```jsonc
{
  "type": "string",       // 必填，MsgType 枚举值之一；不在集合内 → error(unknown_type)
  "id": "string",         // 可选，请求/应答关联用（HITL 请求-应答配对）
  "session": "string",    // 可选，会话作用域（服务端回包会自动带上对应 session）
  "payload": {}           // 必填（可为空对象），消息体
}
```

解析规则（`parse_message`）：
- 非 JSON 对象 → 连接级错误。
- `type` 缺失或不在 `MsgType` 有效集合 → 服务端回 `error`（`code:"unknown_type"`）。
- 单条消息处理异常被捕获，连接不断开，仅回 `error`（`code:"handler_error"`）。

---

## 3. 消息类型总表

> 方向：**C→S** = 客户端发服务端；**S→C** = 服务端发客户端；**双向** = 同一 `type` 既作请求又作应答（靠 `id` 配对）。

| type | 方向 | 说明 |
|---|---|---|
| `hello` | C→S | 握手，可重复调用 |
| `welcome` | S→C | 握手应答 |
| `session.new` | C→S | 新建会话（自动 attach） |
| `session.created` | S→C | 新建成功 |
| `session.attach` | C→S | 附加到已有（持久化）会话 |
| `session.switch` | C→S | 切换到另一会话（保留当前） |
| `attached` | S→C | attach/switch 成功 |
| `session.detach` | C→S | 解除 attach |
| `detached` | S→C | 解除成功 |
| `session.list` | C→S | 列出会话 |
| `session_list` | S→C | 会话列表应答 |
| `task.send` | C→S | 提交用户任务（触发 Agent 运行） |
| `answer` | C→S | 应答 `ask` / `show_questions` 的澄清问题 |
| `confirm_plan` | 双向 | S→C 请求批准计划；C→S 回传批准结果 |
| `approve` | 双向 | S→C 请求授权执行（Action）；C→S 回传授权结果 |
| `command` | C→S | 向会话发送斜杠命令（如 `/switch`） |
| `task.cancel` | C→S | 取消当前在飞的生成（M9.9） |
| `task.cancelled` | S→C | 已停止生成（M9.9） |
| `session.delete` | C→S | 彻底删除会话（M9.9） |
| `session.delete_resp` | S→C | 删除结果（M9.9） |
| `session.title` | C→S | 手动设置会话标题（M11.6） |
| `session.title_resp` | S→C | 标题设置结果（M11.6） |
| `skill.update` | C→S | 技能开关（M11.6） |
| `skill.update_resp` | S→C | 技能开关结果（M11.6） |
| `agent.update` | C→S | 编辑智能体配置（M11.6） |
| `agent.update_resp` | S→C | 智能体编辑结果（M11.6） |
| `session.info` | S→C | 推送 plan_mode / model（M9.9） |
| `trace.list` | C→S | 列出 trace |
| `trace_list` | S→C | trace 列表应答 |
| `trace.get` | C→S | 获取某 trace 的 span 树 |
| `trace_tree` | S→C | span 树应答 |
| `event` | S→C | 实时事件流（核心渲染数据源） |
| `replay_start` | S→C | 历史事件回放开始 |
| `replay_end` | S→C | 历史事件回放结束 |
| `ask` | S→C | 单条澄清问题（HITL，需 `answer` 应答） |
| `show_questions` | S→C | 多条澄清问题展示 |
| `show_plan` | S→C | 展示计划 |
| `show_skills` | S→C | 展示可用技能 |
| `show_agents` | S→C | 展示可用子 Agent |
| `show_tools` | S→C | 展示已注册真实工具清单（M11.6） |
| `notify` | S→C | 轻量通知文本 |
| `close` | S→C | 一轮任务结束 |
| `error` | S→C | 错误 |

---

## 4. 客户端 → 服务端（C→S）

### 4.1 `hello`
- **payload**：`{ "token": string }`（可选；仅当 daemon 配置了 token 时必填）
- **行为**：校验 token（若配置）；幂等，可重复调用以获取当前会话状态。
- **应答**：`welcome`（见 5.1）。

### 4.2 `session.new`
- **payload**：`{ "name": string|null, "project_root": string }`（可选；缺省用 daemon 进程 cwd）
- **行为**：在 `project_root` 下新建会话，自动 attach（绑定当前连接）。
- **应答**：`session.created` + `attached`（见 5.2 / 5.4）。

### 4.3 `session.attach`
- **payload**：`{ "session_id": string, "project_root": string }`（可选）
- **行为**：附加到已持久化会话；若不存在或无法解析 → `error`（`code:"no_session"`）。
- **应答**：成功 → `attached` + `session.info` + 历史回放（`replay_start`…`replay_end` + `event`）。

### 4.4 `session.switch`
- **payload**：`{ "session_id": string, "project_root": string }`（可选）
- **行为**：切换到另一会话（保留当前连接上下文）；失败 → `error`（`code:"no_session"`）。
- **应答**：`attached` + `session.info` + 回放。

### 4.5 `session.detach`
- **payload**：`{}`
- **应答**：`detached` `{ "session_id": string }`。

### 4.6 `session.list`
- **payload**：`{ "project_root": string }`（可选）
- **应答**：`session_list`（见 5.5）。

### 4.7 `task.send`
- **payload**：`{ "text": string, "files"?: string[], "session"?: string, "yes"?: bool, "plan"?: bool }`
  - `text`：用户任务内容（空 → `error` `bad_payload`）。
  - `yes` / `plan`：快捷跳过确认（由 daemon 内部透传给 `Session.step`）。
- **前置**：必须已 attach（`conn.session_id` 非空），否则 `error`（`code:"no_session"`）。
- **行为**：定位会话句柄，置 `busy` 后异步运行 `Session.step`，事件经 `event` 实时下发；结束发 `close`。若会话正忙 → `error`（`code:"busy"`）。

### 4.8 `answer`
- **payload**：`{ "id": string, "text": string }`
  - `id`：对应 `ask` / `show_questions` 的请求 id（客户端需自行维护；当前实现按单条 `id` 配对）。
- **行为**：`transport.resolve(id, text)` 唤醒等待中的 `ask` 协程。

### 4.9 `confirm_plan`
- **payload**：`{ "id": string, "confirmed": bool }`
  - `id`：来自 S→C `confirm_plan` 请求的 `id`。
- **行为**：`transport.resolve(id, confirmed_bool)` 唤醒 `confirm_plan` 协程。

### 4.10 `approve`
- **payload**：`{ "id": string, "approved": bool }`
  - `id`：来自 S→C `approve` 请求的 `id`。
- **行为**：`transport.resolve(id, approved_bool)` 唤醒 `approve` 协程。

### 4.11 `command`
- **payload**：`{ "name": string, "args": string|null, "project_root"?: string }`
  - `project_root`：可选（M11.5），用于无 attach 会话时的全局只读查询（`skills` / `agents`）。
- **行为**：交给 `dispatch_command`（如 `name="switch"` 触发会话切换）；未识别命令 → `notify`「未知命令」。
- **无 attach 会话时的特殊分支**：`skills` / `agents` 是**全局只读查询**，不依赖具体会话——即使未 attach 也用 `project_root` 直接构造 loader 返回清单（`show_skills` / `show_agents`），不再报 `no_session`；`tools` 同样全局可查（`show_tools`）；其余命令仍返回 `error`（`code:"no_session"`）。

### 4.12 `task.cancel`
- **payload**：`{}`
- **行为**：取消当前会话在飞的 `step` 任务（`running_task.cancel()`）；无在飞任务则无操作。

### 4.13 `session.delete`
- **payload**：`{ "session_id": string, "project_root"?: string }`
- **应答**：`session.delete_resp`（见 5.9）。删除会话事件 / Session Memory / trace + 内存句柄级联。

### 4.14 `trace.list`
- **payload**：`{ "project_root": string, "session_id"?: string }`
- **应答**：`trace_list`（见 5.17）。按 trace_id 分组返回（一个会话可能有多次用户操作，各一条）。

### 4.15 `trace.get`
- **payload**：`{ "project_root": string, "trace_id": string }`（trace_id = message_id）
- **应答**：`trace_tree`（见 5.18）。

### 4.16 `session.title`（M11.6）
- **payload**：`{ "session_id"?: string, "project_root"?: string, "title": string }`
  - `session_id` 缺省时用当前 attach 的会话。
- **行为**：用户手动设置会话标题（来源 `manual`，**优先级最高**，持久化到 `SessionStore`；同时同步内存 handle.name）。标题会 trim 并截断到 60 字符。
- **应答**：`session.title_resp`（见 5.16b）。

### 4.17 会话标题的自动生成（M11.6）
- 标题优先级（低→高）：**用户首个提问 → session memory 的 Session Title → 用户手动设置**。
- 首个提问在 `task.send` 时捕获（仅当该会话尚无标题时写入，来源 `user`）。
- session memory 摘要更新后，若含 `## Session Title` 段，则落盘为会话标题（来源 `memory`；**不覆盖**用户手动设置的标题）。
- 用户手动设置（`session.title`）永不被自动覆盖。

### 4.18 `skill.update`（M11.6）
- **payload**：`{ "project_root": string, "name": string, "enabled": boolean }`
- **行为**：技能开关——写回 `<skill>/SKILL.md` 的 frontmatter `disable_model_invocation`（`enabled=true` → 模型可自动调用；`false` → 仅手动 `/name`）。找不到技能返回 `ok:false`。
- **应答**：`skill.update_resp`（见 5.16c）。

### 4.19 `agent.update`（M11.6）
- **payload**：`{ "project_root": string, "name": string, "updates": object }`
  - `updates` 支持字段（对齐 `AgentSpec`）：`description` / `tools`（数组或 null=继承全部）/ `model` / `permission_mode` / `max_turns` / `disallowed_tools` / `share_history` 等；`system_prompt` 会作为正文写回。
- **行为**：编辑智能体——把 `updates` 合并进该 agent 的 `.md` frontmatter 并写回。**仅非内置**（用户/项目级）可编辑；内置（explore/plan 等）返回 `ok:false`。
- **应答**：`agent.update_resp`（见 5.16d）。

---

## 5. 服务端 → 客户端（S→C）

### 5.1 `welcome`
- **payload**：`{ "daemon_version": "1.0.0", "protocol_version": "3.1" }`

### 5.2 `session.created`
- **payload**：`{ "session_id": string, "name": string|null, "project_root": string }`，信封带 `session`。

### 5.3 `attached`
- **payload**：`{ "session_id": string, "project_root": string }`，信封带 `session`。
  - `session.switch` 成功时同样用本类型下发（字段相同）。

### 5.4 `detached`
- **payload**：`{ "session_id": string }`

### 5.5 `session_list`
- **payload**：`{ "project_root": string, "sessions": SessionInfo[] }`
- **SessionInfo 字段**（见 6.6）：
  ```jsonc
  { "id": string, "name": string|null, "title": string|null, "project_root": string,
    "attached": bool, "running": bool, "last_activity": number|null,
    "persisted"?: bool }   // 持久化会话带 persisted:true
  ```
  - `title`（M11.6）：显示标题（首个提问 / session memory / 用户手动，持久化）；前端优先展示，缺省回退 `name` / `id[:8]`。

### 5.6 `event`
- **payload**：`{ "event": Event.to_dict() }`，子会话事件额外带 `subsession_id`：`{ "event": {...}, "subsession_id": string }`。
- **后台 subsession 标记**：后台子 agent（如 session-memory 记忆子 agent）的事件，其 `background: true` 在 `event` **结构体内部**（经 `to_dict()` 序列化），随回放/持久化完整保留；前端据此**不渲染进前台聊天区**。信封层不额外带 `background` 字段。
- 这是前端渲染的核心数据源（见 6.1 事件结构）。

### 5.7 `replay_start` / `replay_end`
- **payload**：`{}`（信封带 `session`）。回放期间批量下发历史 `event`（仅非 `transient` 事件）。

### 5.8 `ask`
- **payload**：`{ "id": string, "question": Question.to_dict() }`（见 6.2）。
- **客户端须应答**：`answer` `{ "id": <同 id>, "text": <回答文本> }`。

### 5.9 `show_questions`
- **payload**：`{ "questions": Question.to_dict()[] }`（见 6.2）。

### 5.10 `show_plan`
- **payload**：`{ "plan": string, "plan_path": string|null, "plan_steps": PlanStep[] }`（见 6.3）。
- 计划批准流程由 `confirm_plan` 消息驱动（见 5.11）。

### 5.11 `confirm_plan`（请求）
- **payload**：`{ "id": string }`，信封带 `id`（与请求配对）。
- **客户端须应答**：`confirm_plan` `{ "id": <同 id>, "confirmed": bool }`。
  - `confirmed=true` → 后端开始执行，发 `notify`「计划已批准，开始执行」。
  - `confirmed=false` → 后端中止，发 `notify`「计划已拒绝，任务中止」+ `close`。

### 5.12 `approve`（请求）
- **payload**：`{ "id": string, "action": Action.to_dict() }`（见 6.4）。
- **客户端须应答**：`approve` `{ "id": <同 id>, "approved": bool }`。
  - `approved=true` → 后端执行该 Action；若 `action.tool` 为命令类会发 `notify`「已授权执行：<name>」。
  - `approved=false` → 发 `notify`「已拒绝执行：<name>」。

### 5.13 `show_skills` / `show_agents`
- **payload**：`{ "specs": [ { "name": string, "title": string, "description": string, ... } ] }`
  - 各 spec 字段由后端对象 `to_dict()`（或公开属性）决定，至少含 `name/title/description`。
  - **`source`（M11.6）**：来源 `builtin` / `user` / `project`（技能无内置，为 `user`/`project`），供前端分组展示。
- **触发**：① 有会话时经 `/skills`、`/agents` 命令；② **无 attach 会话时**（M11.5）经 `command` 的 `skills`/`agents` 全局查询分支——按 `project_root` 直接构造 `SkillLoader` / `SubagentSpawner` 返回清单，无需先建会话。

### 5.14 `show_tools`（M11.6）
- **payload**：`{ "tools": [ { "name": string, "risk": string, "description": string } ] }`
  - `risk`：`read` / `edit` / `exec`。
  - 数据源：当前进程 `default_registry.list()`（真实注册工具），与 LLM 实际可调用的工具一致。
- **触发**：经 `command` 的 `tools` 全局查询分支（无 attach 会话即可），用于前端渲染「工具白名单」勾选项，避免前端硬编码与后端不一致。

### 5.15 `notify` / `close` / `task.cancelled`
- `notify`：`{ "message": string }`。
- `close`：`{}`（信封带 `session`），表示一轮 `task.send` 运行结束（含正常完成 / 计划拒绝中止 / 取消后清理）。前端须保证在收到 `close` 后再处理最终 `event`，以避免顺序错乱（服务端已用发送锁保证 FINAL 先到）。
- `task.cancelled`：`{}`，生成被 `task.cancel` 真实中断后下发。

> **M10.3**：`usage` 不再是独立消息类型，改为随 `event` 消息下发 `USAGE` 子类型事件（见 5.6 / 6.1 / 6.7）。前端经 `event` 消息的 `event.type === "usage"` 读取，payload 见 6.7。

### 5.15 `session.info`（M9.9）
- **payload**：`{ "plan_mode": bool, "model": string }`，attach/switch 后下发，供前端顶栏与输入区展示。

### 5.16 `session.delete_resp`（M9.9）
- **payload（成功）**：`{ "ok": true, "session_id": string }`
- **payload（失败/缺参）**：`{ "ok": false, "message": string }`（缺 `session_id`）或 `{ "ok": false, "session_id": string, "message": string }`。

### 5.16b `session.title_resp`（M11.6）
- **payload（成功）**：`{ "ok": true, "session_id": string, "title": string }`
- **payload（失败/缺参）**：`{ "ok": false, "session_id"?: string, "error"?: string }`。

### 5.16c `skill.update_resp`（M11.6）
- **payload（成功）**：`{ "ok": true, "name": string, "enabled": boolean }`
- **payload（失败）**：`{ "ok": false, "name": string, "error": "skill_not_found" }`。

### 5.16d `agent.update_resp`（M11.6）
- **payload（成功）**：`{ "ok": true, "name": string }`
- **payload（失败）**：`{ "ok": false, "name": string, "error": "agent_not_editable" }`（内置或未找到）。

### 5.17 `trace_list`
- **payload**：`{ "project_root": string, "traces": TraceInfo[] }`
  - `TraceInfo`：`{ "trace_id": string, "session_id": string, "span_count": number, "first_ts": number|null, "last_ts": number|null }`（trace_id = message_id，即一次用户操作的唯一标识）。

### 5.18 `trace_tree`
- **payload**：`{ "trace_id": string, "session_id": string|null, "spans": SpanNode[] }`（见 6.8）。trace_id 即请求时的 trace_id，session_id 从存储的 span 中还原。

### 5.19 `error`
- **payload**：`{ "code": string, "message": string }`。
- **常见 code**：见第 7 节。

---

## 6. 嵌套数据结构

### 6.1 Event（`event` 消息体内的 `event` 字段，`Event.to_dict()`）
固定字段：`seq`(int)、`type`(EventType 字符串)、`ts`(float，Unix 秒)。`type` 取值：
`decision` / `clarify` / `plan` / `plan_progress` / `tool_use` / `tool_result` / `file_original` / `final` / `error` / `text` / `tool_call_delta` / `user` / `usage`。

按类型出现的可选字段（仅非空时包含）：
| 字段 | 类型 | 出现于 |
|---|---|---|
| `decision` | `{ text?:string, tool_calls: ToolCall[] }` | `decision` |
| `tool_use` | `{ id, name, arguments }` | `tool_use` |
| `tool_result` | `{ ok:bool, output?:string, error?:string, diff?:string, original?:string }` | `tool_result`（write/edit 回传 `diff`/`original`） |
| `tool_call_id` | string | `tool_result` 等 |
| `tc_index` / `tc_name` / `tc_args` | int / string / string | `tool_call_delta`（瞬时，不回放） |
| `file_path` / `file_original` | string / string | `file_original`（瞬时，不回放；write/edit 流式预读原内容，供前端实时 diff） |
| `background` | bool | 任意事件（后台 subsession，如 session-memory 记忆子 agent；进 event 序列化，回放/持久化仍保留，前端据此不渲染进前台聊天区） |
| `text` | string | `text` / `final` / `error` |
| `kind` | `"reasoning"` \| `"content"` | `text`（区分思考/输出） |
| `error` | string | `error` |
| `questions` | `Question[]` | `clarify` |
| `plan_path` | string | `plan` / `plan_progress` |
| `plan_update` | `{ step_id, status, note? }` | `plan_progress` |
| `subsession_id` | string\|null | 任意（子会话事件在信封层已带，结构体本身不重复） |
| `transient` | bool | 仅瞬时事件标记（delta 类），不进回放缓冲 |
| `usage` | `{...}`（见 6.7） | `usage`（USAGE 事件，非瞬时，入档并回放） |
| `estimated` | bool | `usage` |
| `duration` | float（秒，墙钟，含 HITL 等待） | `usage` |
| `message_id` | string | `usage`（逐 message 归集用量） |
| `parent_message_id` | string\|null | `usage`（子 agent 用量归集回派生子 agent 的 message） |

> `tool_call_delta` / `file_original` 为瞬时事件：实时转发但不入档、不回放。

### 6.2 Question（`ask` / `show_questions` / `clarify` 内）
```jsonc
{ "question": string, "options"?: string[], "multiSelect"?: bool }
```
（仅 `options`/`multiSelect` 为真值时出现）

### 6.3 PlanStep（`show_plan.plan_steps`）
```jsonc
{ "id": string, "title": string, "status": string, "detail"?: string|null }
```
`status` 取值见 `PlanStatus`（如 `pending` / `executing` / `done` / `failed`）。

### 6.4 Action（`approve.action`）
```jsonc
{ "tool": string, "risk": string, "args": object,
  "description": string, "approval_request": string }
```

### 6.5 ToolCall（`decision.tool_calls` / `tool_use`）
```jsonc
{ "id": string, "name": string, "arguments": object }
```

### 6.6 SessionInfo（`session_list.sessions`）
见 5.5。

### 6.7 Usage（`event` 的 `USAGE` 子类型，M10.3 起取代独立 `usage` 消息）
随 `event` 消息下发（`payload.event.type === "usage"`），非瞬时（入档并回放）。
```jsonc
{ "type": "usage",
  "usage": {
    "prompt_tokens"?: int, "completion_tokens"?: int, "total_tokens"?: int,
    "reasoning_tokens"?: int,
    "cache_hit_tokens"?: int, "cache_miss_tokens"?: int, "cache_write_tokens"?: int,
    "estimated_tokens"?: int },   // 无真实用量时为估算值
  "estimated": bool,             // 无真实用量时为 true
  "duration": float,             // 本次响应墙钟耗时（秒，含 HITL 等待）
  "message_id": string,          // 归属 message（逐 message 归集用量）
  "parent_message_id"?: string } // 子 agent 指向派生子 agent 的 message（message 树）
```

### 6.8 SpanNode（`trace_tree.spans`）
```jsonc
{ "span_id": string, "name": string, "kind": string,
  "parent_id": string|null, "started_at": number, "ended_at": number|null,
  "status": "open"|"ok"|"error",               // M5.5: error 表异常退出
  "meta": object,                               // 含 trace_id / message_id / user_text / usage 等
  "logs": [ { "ts": number, "key": string, "value": any, "level": string } ] }
```

---

## 7. 错误码（error.code）

`protocol.ERROR_CODES` 定义的标准集合：`ok` / `unknown_message` / `bad_payload` / `unknown_session` / `no_session` / `budget_exceeded` / `internal` / `cancelled` / `not_found`。

运行时实际还会用到以下 code（不在上述枚举但已落地）：
| code | 触发场景 |
|---|---|
| `auth` | `hello` 的 token 不匹配 |
| `no_session` | 未 attach / 会话不存在 / 会话未初始化 |
| `no_transport` | 会话无 transport |
| `busy` | 会话正忙（`task.send` 并发） |
| `handler_error` | 单条消息处理抛异常（连接级兜底） |
| `unknown_type` | `type` 不在 MsgType 集合 |
| `trace_error` | trace 存储读取异常 |
| `no_session` / 其它 | `session.attach` / `session.switch` 失败 |

---

## 8. 典型交互流程

### 8.1 新建会话并提交任务
```
C→S hello                          → S→C welcome
C→S session.new {name}             → S→C session.created + attached
C→S task.send {text}               → S→C event*(decision/tool_use/tool_result/final)
                                    → S→C usage
                                    → S→C close
```

### 8.2 澄清（ask / answer）
```
S→C ask {id, question}             → C→S answer {id, text}
```
（多问场景用 `show_questions` + `answer` 配对同一 `id`。）

### 8.3 计划批准
```
S→C show_plan {plan, plan_steps}   → S→C confirm_plan {id}
C→S confirm_plan {id, confirmed}   → (true)  S→C notify + 继续 event 流
                                    → (false) S→C notify + close
```

### 8.4 执行授权（approve）
```
S→C approve {id, action}           → C→S approve {id, approved}
                                    → (true)  S→C notify「已授权执行」+ 执行
                                    → (false) S→C notify「已拒绝执行」
```

### 8.5 附加历史会话（回放）
```
C→S session.attach {session_id}    → S→C attached
                                    → S→C session.info
                                    → S→C replay_start
                                    → S→C event*(历史，非 transient)
                                    → S→C replay_end
```

### 8.6 取消生成
```
C→S task.cancel {}                 → (运行中) S→C notify「正在停止生成」
                                    → S→C task.cancelled + close
```

### 8.7 可观测面板
```
C→S trace.list {project_root}      → S→C trace_list {traces}
C→S trace.get {trace_id}           → S→C trace_tree {spans}
```

---

## 9. 版本与契约一致性

- **`PROTOCOL_VERSION = "3.1"`**：WebSocket 子协议版本。变更需评估前端兼容并同步 `types.ts` 与桌面端 WS 客户端。
- **`DAEMON_VERSION = "1.0.0"`**：随 `welcome` 下发，便于前端判断能力。
- **契约测试**：`scripts/check-msgtype.mjs`（`tests/unit/test_m9_protocol_contract.py` 通过 `node` 调用）比对 `protocol.py` 的 `MsgType` 值与 `types.ts` 的 `ALL_MSG_TYPES`，不一致则 CI 失败。**新增/删除消息类型必须两端同步**。
- **前端对齐点**：`desktop/src/protocol/types.ts`（`ALL_MSG_TYPES`、`Envelope`、`AgentEvent` 等强类型投影）须与本文件保持一致。

---

## 10. 待对齐项（drift，建议排期修复）

> 以下为 `protocol.py` docstring 与 `server.py` / `bridge.py` 真实实现的差异，本文档已按**真实实现**编写。请后端择机修正 `protocol.py` 注释，使两者一致：

1. **`hello` / `welcome` 字段**：`protocol.py` 注释写 `hello` 带 `client`/`version`、`welcome` 带 `session`；真实实现 `hello` 用 `token`、`welcome` 仅 `daemon_version`/`protocol_version`（无 `session`）。
2. **`confirm_plan` 应答字段**：`protocol.py` 注释写 `{"approved": bool}`；真实实现客户端回传 `{"confirmed": bool}`。
3. **`approve` 应答字段**：`protocol.py` 注释写 `{"id","decision":"allow"|"deny"}`；真实实现客户端回传 `{"id","approved": bool}`。
4. **`answer` 字段**：`protocol.py` 注释写 `{"answers":[{question,answer}]}`；真实实现为 `{"id": string, "text": string}`（单条配对）。
5. **`command` 语义**：`protocol.py` 注释暗示「命令执行中输入」；真实实现为向会话派发斜杠命令（`/name args`），无独立输入队列。
6. **`show_plan` 状态**：`protocol.py` 注释暗示带 `status` 字段；真实实现仅 `plan`/`plan_path`/`plan_steps`，状态靠 `confirm_plan` 流程表达。

---

## 11. Daemon 进程分发（方案 A：冻结二进制）

WebSocket 协议本身不变；本节仅说明**前端如何拉起 daemon 子进程**。

- **本地开发**：仍走 `python -m agent.cli daemon`（由 `desktop/src/main/daemon.ts` 在 `app.isPackaged === false` 时调用 `locatePython()`）。
- **打包分发（方案 A）**：CD 流水线用 PyInstaller 把 `agent.cli daemon`（`agent/daemon_launcher.py` 入口）冻结为独立二进制 `daemon[.exe]`，经 `electron-builder` 的 `extraResources` 打入安装包 `resources/daemon/`。打包态下 Electron 优先拉起该二进制，**无需主机安装 Python**。
- **优先级**：`AGENT_DAEMON_BIN`（显式覆盖） > 打包态冻结二进制（`app.isPackaged` 且文件存在） > 本地 Python 路径。
- **启动日志契约不变**：冻结二进制仍打印 `ws=... health=...`，故 `parseDaemonLog` 与 `/health` 轮询逻辑无需改动。

构建与分发流水线见 `.github/workflows/cd.yml`；冻结脚本见 `scripts/build_daemon.py`。

---

*维护人：后端 daemon 负责同学。任何接口变更请同步：本文件 + `agent/daemon/protocol.py` + `desktop/src/protocol/types.ts` + 契约测试。*
