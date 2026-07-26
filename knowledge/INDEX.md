# 项目知识库索引（knowledge/INDEX.md）

> 跨里程碑共享知识。开始新步骤前先读本文件恢复上下文。
> 维护规则：只记后续会用到且易忘的**架构/约定/决策/坑**；接口签名、可重新生成的代码不记（以代码为准）。
> 本文件仅保留「现状」与「可能会踩的坑」，历史变更见各 `milestones/<M>/` 文档。

---

## 架构决策（现状，来源：调研报告）

- **98/1.6 法则**：AI 只做决策，循环/权限/路由/压缩/持久化全部确定性实现且可独立测试。
- **安全在 OS 层**：沙箱是独立可插拔执行层（Local seccomp / Docker），prompt 仅软约束。完整设计见 `knowledge/sandbox-approval-design.md`。
- **上下文稀缺**：静态(系统提示/规则) 与 动态(对话/工具结果) 分离；稳定前缀走 prompt caching；超阈值递归摘要。
- **能力正交**：Tool(原子) / Skill(按需包) / Subagent(隔离上下文) 三层。
- **两条全局主线**：事件流（状态单一事实来源）+ Trace/Span（OTel 语义，父子 parent_id）。
- **可恢复**：检查点用 `session_id` + sqlite，路径 `<project>/.agent/sessions/<id>/`。

## 设计文档索引（standalone，现状）

- **上下文管理设计**：`knowledge/context-management.md`（工具结果保存 vs 注入、双轨映射、四层压缩防线、配对铁律、子代理隔离）。面向用户介绍见 `docs/上下文与记忆体系介绍.md`。
- **沙盒与审批设计**：`knowledge/sandbox-approval-design.md`（Codex 模式：local/docker/external + 三档 profile + 网络默认拒绝 + AskForApproval 四模式）。面向用户介绍见 `docs/沙箱体系介绍.md`。
- **Claude Code Subagents + Skills**：`knowledge/claude-code-subagents-skills.md`（映射到本项目既有接口）。
- **Claude Code 上下文管理机制**：`knowledge/claude-code-context-management.md`（四层渐进压缩防线、Session Memory 深度机制）。
- **行业调研**：`knowledge/调研-agent-cli渲染与runner交互.md`、`调研-CLI美化方案.md`、`调研-Textual全屏CLI重构方案.md`。

## 工程约定（现状）

- 语言 Python 3.12+；CLI 用 typer；异步 asyncio；配置 pydantic-settings + YAML 分层。
- LLM 一律可 Mock：`Model` 抽象 + `FakeModel`/`RecordingModel`，测试不依赖真实 API。
- 目录：`agent/core`(循环/意图/模型/传输/事件) / `agent/runtime`(注册/审批/沙箱) / `agent/context`(管理/压缩/记忆/会话存储) / `agent/skills` / `agent/subagent.py` / `agent/resilience`(韧性层) / `agent/obs`(可观测) / `agent/config` / `agent/daemon`(守护进程) / `tools/` / `skills/` / `tests/`(unit|integration|e2e)。
- CI 强制：`ruff check .` + `ruff format --check .` + `basedpyright agent/` 全绿；禁止 lambda 赋值（E731）；git commit/push 需用户显式批准。

## 环境与 Provider（现状）

- **provider 无关**：统一走 OpenAI 兼容协议（`/v1/chat/completions`）。换 API 只改 `<项目根>/.agent/settings.yaml` 的 `llm.api_key` / `llm.base_url` / `llm.model`，不动代码。默认值指向 DeepSeek（`https://api.deepseek.com`，模型 `deepseek-v4-flash`）。
- 配置加载：`pydantic-settings` + 自定义 `YamlConfigSource` 读 YAML（用户级 + 项目级）；**不读 `.env`**；CLI 参数优先级最高。
- **流式**：`Model.stream(messages) -> AsyncIterator[StreamEvent]`（`text` 增量 + `done` 回传完整 `Decision`）。
- 测试一律用 `FakeModel` / `RecordingModel`，或向 `OpenAICompatibleModel` 注入假 client（不联网）。

---

## 核心架构现状（跨里程碑）

### 事件流 + 传输层（AgentTransport）
- `EventStream`（`agent/core/events.py`）是唯一实时线格式：`subscribe(sink)` 同步分发；`append` 入档、`emit` 仅分发不入档（`transient=True`，用于 `tool_call_delta` 预览，不污染持久化序列与回放）。`Event` 类型集合：`decision|clarify|plan|plan_progress|tool_use|tool_result|final|error|text|tool_call_delta|usage`（M10.1 新增 `usage` 用量落盘事件，非 transient）。
- `AgentTransport`（`agent/core/transport.py`）统一协议：HITL 方法 + `bind(stream)`（订阅自行渲染）+ `close()` + `report_usage()`。CLI `TerminalTransport`、TUI `TextualTransport`、daemon `BridgeTransport` 实现同一协议。
- **铁律**：新增实时渲染走事件（持久化用 `append`、瞬时预览用 `emit`），**不要再给 loop 加 `presenter` 回调参数**。

### daemon（client-server 常驻 + 子会话多路复用）
- daemon（`SessionRegistry` + `BridgeTransport` + WS/HTTP server）常驻，前端（CLI/TUI/Web）仅渲染+输入，经 WebSocket 协议交互。core 层零/极小改动。
- 通信协议铁律：事件直接复用 `Event.to_dict()/from_dict()` 经 `event` 消息转发；HITL 由服务端 `BridgeTransport` 封装为带 `id` 的请求、`await Future`，客户端回传同 `id` 应答唤醒。
- 每会话定长环形缓冲 `deque(maxlen=200)` 支撑回放（**仅收非 `transient` 事件**，避免 `tool_call_delta` 重复）；session 切换=detach+attach。
- **子会话多路复用（M9）**：daemon 模式子 agent 用独立 `EventStream`（独立 seq 空间）+ `SubsessionBridgeTransport`，事件经**父 `attached_conn`** 发出，`EVENT` payload 带 `subsession_id`（格式 `<parent>/sub_<agent>_<depth>_<uuid>`）；前端按 id 分桶渲染，子事件**不并进父 `event_buffer`**，父 reducer 不被污染。CLI/非 daemon 模式保持本地 `_SubAgentTransport`/`_SubAgentTuiTransport` 渲染，行为不变。

### 会话持久化（SessionStore）+ 回放
- sqlite 库：`<project>/.agent/sessions/sessions.db`（`sessions` + `events` 表）。`events` 列：`session_id, seq, type, json, transient, ts, parent_session_id`（`parent_session_id` 用于把子会话事件挂到父会话，回放可全量找回）。
- `SessionStoreSink` 订阅 `EventStream` 零侵入落盘非 transient 事件；`loop.run` 经 `event_sink` 注入；`Session.from_store` 从持久化重建 `messages` + 持有 `event_stream`。
- `iter_events_with_subsession(session_id)`：`UNION ALL` 查父事件（sub=NULL）+ 子事件（sub=session_id），按 `ts, seq` 排序返回全量。
- daemon `_replay` 优先读 sqlite 全量回放（不再只走内存缓冲，避免长会话重进历史变少）。
- **⚠️ 落盘 seq 全局性**：`SessionStoreSink` 必须用「会话级全局 seq」覆盖 per-run seq（新 run `EventStream` 从 0 起；否则 `INSERT OR REPLACE` 主键 `(session_id,seq)` 覆盖父前缀）。回放流是派生视图，序号由 daemon 统一编排，严禁信任各轮自带 `seq`。
- **⚠️ `_init_schema` 迁移顺序**：新增列时 `CREATE INDEX ... ON events(parent_session_id)` 必须在 `ALTER TABLE ... ADD COLUMN` **之后**（旧库执行 `executescript` 时建索引先失败会中止整个脚本，ALTER 永不执行）。

### 上下文压缩（四层渐进防线）
- 双轨：`EventStream` 全量不可变（审计/压缩派生源）vs `conv`/`Session.messages` 可压缩投影（注入）；压缩只作用于 `conv`，绝不碰 `EventStream`。**配对铁律**：`tool_use`+`tool_result` 成对，禁止孤立任一方。
- 四层：`Microcompact`（零成本占位符 `[Old tool result content cleared]`，保留最近 N 条）→ `Auto Compact`（AI 9 段摘要，返回新列表）→ `Session Memory`（后台记忆子 agent 产出摘要，零 API 调用）→ Reactive Compact。`ContextManager.compact()` 优先 Session Memory，否则 Auto 兜底。
- 固定底座 `AGENTS.md`：静态/动态分离，稳定前缀在前复用 prompt cache；永不压缩，每次投影重读注入。
- `EventType.USER` 已加入 EventStream（每轮开头 emit），使事件流成为完整可重放转录（重建 messages 与崩溃前一致）。

### Subagent（隔离 + 实时展示）
- 复用无状态 `AgentLoop`：只传独立 `messages=[]` 即隔离上下文；可注入不同 model/registry/sandbox/gate。
- **Trace 父子铁律**：子 agent 经 `SubagentSpawner.spawn(..., parent_span=父span)` 调起，`AgentLoop.run(name=spec.name)` 用 `parent=parent_span` 挂到父链；span 名 `agent.run:{name}`。
- 深度限制 5（`depth>=max_depth` 抛 `RecursionError`，每次 +1）；工具白名单（子 registry = base 子集）；内置 `explore`/`plan`(只读)/`general-purpose`。
- 后台子 agent：`Session.spawn_background` 复用正在运行的事件循环推进；`share_history=True` 的内置 `session-memory` 子 agent 只产出文本、由父落盘，**绝不碰项目文件**。
- 主聊天区按 `subsession_id` 连续分段成独立 `SubagentBlock`；后台面板复用同一 `SubagentCard` 渲染，完成后仍可回看历史。

### 桌面端（Electron + Vite + React + TS）
- 主进程 `DaemonManager` 单例 spawn/守护全局单一 daemon；`contextBridge` **只读**注入 ws 配置（`getWsUrl/getToken/getHealthUrl`），**绝不**走 `loadURL` query（避免 token 进地址栏）；renderer 直连 daemon。
- `desktop/src/protocol/` 实现与 `agent/daemon/protocol.py` 对齐的 TS 消息类型 + `DaemonClient` + `parseEvent`；`desktop/scripts/check-msgtype.mjs` 契约测试（解析两源文件比对 MsgType 集合，漂移即失败）。
- 聊天渲染：`useEventReducer.ts` 的 `buildChatModel` 把 `AgentEvent[]` 归约为视图模型（流式 Markdown + 可折叠工具卡 + diff 高亮）；主聊天按 `subsession_id` 分段出 `SubagentBlock`，子 agent 块内工具调用默认折叠。
- 会话状态机 `sessionMachine.ts`（`SessionsState`，纯 reducer 可单测）；replay 缓冲 `replay.ts`（`liveEvent` 守卫：replaying/无 active 时不追加，避免瞬时事件重复渲染）。
- 可观测面板 `features/obs/`：状态栏 + Trace 树（按 `parent_id` 重建）+ 日志 + 后台子 Agent 面板；daemon 经 `trace.list`/`trace.get` 按 `project_root` 隔离读 `TraceStore`。

### 测试金字塔 + CI（现状）
- 三层：`tests/unit`(纯逻辑 mock) / `tests/integration`(daemon+schema 快照+tool tapes，确定性不调真 LLM) / `tests/e2e`(慢非确定，全 `@pytest.mark.slow`)。`pyproject` `addopts="-m 'not slow'"`。
- `agent/testing/recorded_model.py` 的 `RecordedModel`（给定 `list[Decision]` FIFO 回放）+ `decisions_from_eventstream` + `dump_tape/load_tape`，Tier2 主力替身。prompt 快照(Tier1) + tool tapes(Tier2) + e2e(Tier3)。
- CI 快慢分离：`fast`=ruff+basedpyright+pytest+cov（push/PR 门禁）；`slow`=nightly e2e（注入真实 key，不阻塞 PR）。真实 LLM 调用只进 `slow`/e2e。

### 常用命令（现状）
- `python -m agent.cli run "<task>"` / `chat`(默认 TUI，非 TTY 退回旧渲染) / `chat --legacy` / `resume <id>` / `fork <id>` / `client --resume`。
- REPL：`/plan` `/exec` `/approve` `/mode` `/context` `/compact` `/skills` `/agents` `/skill <name>` `/agent <name> <task>` `/bg` `/resume <id>` `/fork <id>` `/help`。
- 桌面端命令面板（Ctrl/Cmd+K）对齐同一命令集。

---

## 踩坑（可能会踩的坑）

> 只保留后续易忘、非代码可推的非显然坑。按领域分组。

### 后端核心：loop / events / 配置
- **空 name 工具调用死循环（易忘）**：模型在「带 `tools` 的纯文本回复」末尾偶尔附带一个 `name` 为空的 `tool_call`（流式协议噪声）。不过滤 → `is_final=False` → 落入执行分支 → 空 name 被当 `UnknownTool` 降级 → 反复刷屏。**修复**：`decision.tool_calls = [tc for tc in ... if tc.name and tc.name.strip()]`。判据：终端只出现纯文本「模型输出」面板却不停重复（非 stall，stall 会抛 `LoopStalled`）。
- **澄清/计划提前返回后再跑报 400**：澄清闸门/`present_plan` 闸门会提前 return 并把 `assistant(tool_calls=[...])` 写进 `conv`，但会话层是把答案作为**新 user 消息**续跑 → `assistant(tool_calls) → user` 违反 OpenAI 协议（带 `tool_calls` 的 assistant 必须紧跟每个 `tool_call_id` 的 tool 回执）。**修复**：提前返回处保留对应控制工具调用并各补一条 `Message(role="tool", tool_call_id=tc.id, content=占位)`。
- **澄清必须用工具而非散文反问**：模糊任务必须用 `ask_clarification` 工具，禁止在 final 文本里散文反问（会被 harness 忽略且浪费轮次）；每条尽量带 `options`。
- **stall 执行次数 = `max_repeat_calls+1`**（执行后判断）；`max_iterations` 是软上限（触顶返回带 `soft_limit_hit` 的 `AgentResult`，不抛异常，会话层可续）。
- **config 分层实现**：自定义 `YamlConfigSource` 的 `__init__` 预合并「用户级→项目级」，`get_field_value` 逐字段返回（pydantic-settings 主循环用 `get_field_value` 而非 `__call__`，这点易踩）；`settings.yaml` 里写 list 字段（如 `plan_mode_bash_allow`）会**整体覆盖**默认列表，别误删既有项。
- **Windows 子进程超时**：`create_subprocess_shell`→`cmd.exe` 派生，仅 `proc.kill()` 留孤儿持管道；正确超时用 `asyncio.wait({communicate_task, sleep(t)}, FIRST_COMPLETED)` 竞速 + Windows `taskkill /F /T /PID`（Unix `proc.kill()`）杀树。

### 上下文压缩（ContextManager / compactors）
- **`AutoCompact.compact` 返回新列表**（与 Microcompact 原地修改不同），`compact()` 用 `new_conv is not self.conv` 判断是否压缩。
- **`should_compact()` 只计量 `conv[compact_boundary:]`**：大消息若在 boundary 之前不会被计量到，集成测试须把大消息放 boundary 之后。
- **失败断路器用 `>=`**（非 `>`）：否则 `failure_count=3` 仍尝试第 4 次。
- **`apply_microcompact` 用 `len(conv)` 而非 `compact_boundary`**：会话初期 `compact_boundary=0` 会使 Microcompact 整段无操作。
- **`compact_boundary=0` 时 AutoCompact 须自动取 `len(conv)-recent_keep`** 压缩更早历史（首超阈值真正生成摘要，否则仅 microcompact 生效）。
- **`_UNSET` 哨兵区分"缺省"与"显式禁用"**：构造器形参默认用模块级 `_UNSET`（而非 `None`），否则 `build_context_manager(x_enabled=False)` 把"禁用"误判为"未指定"而重新构造默认实例。
- **压缩 span 必须挂 `Session.root_span` 下**：否则 microcompact span 成新 root，破坏「每 Session 仅一个 root span」契约。
- **`ContextUsage` 无 `effective_window` 字段**（在 `ContextManager` 上），打印当前占用取 `session.context_mgr.effective_window`。
- `/context` 动态段恒为 0 → 须把每次构建 system prompt 时估算的 static/dynamic token 回填 `context_mgr._system_fixed/_system_dynamic`。

### Subagent / daemon
- **后台子 agent 异步 REPL 共享 loop**：`chat` 用 `asyncio.run(_chat_repl())`，前台 `await session.step` 与后台 `ensure_future` 共享同一运行中的 loop；**非 TTY 必须退化为同步 `typer.prompt`**（按 `sys.stdin.isatty()` 判定，否则 `prompt_async` 卡死）。
- **后台 Subagent 渲染 `live=False`**：顶层 rich `Live` 与 `prompt_toolkit.prompt_async` 输入行不能共存同一终端（抢光标转义序列 → 横幅叠影/输入乱码）。前台 spawn（step 内执行、ptk 未活动）仍 `live=True`。**坑**：布尔开关原名 `self._live` 与父类 `TerminalTransport._live`（流式 Live 实例）冲突，已改名 `self._use_live`，勿改回。
- **`AgentLoop.__init__` 必须显式初始化 `self._agent_span=None` / `self._transport=None`**（在 `run()` 内设置；直接调 `_tool_spawn_subagent` 测试路径时需回退 None）。
- **daemon 就绪竞态**：`ws=...` 就绪日志必须在 `_serve` 的 `create_ws_server` 块**内**打印（否则 `DaemonManager.waitForReady` 在端口未监听时误判就绪）。
- **store 按项目隔离 + 路径锚定**：相对 db 路径（如 `obs.sessions_db_path`）默认相对 cwd，多项目会串库；daemon 内 `_anchor_path(p, project_root)` 锚定到项目根。
- **`SessionStore` 回放修复**：旧 `_replay` 只走内存 `event_buffer`（被 maxlen 截断）→ 长会话重进历史变少、子事件从未落盘；现改读 sqlite 全量 + 子事件带 `parent_session_id` 落盘。

### 用量持久化（M10：message 模型 + USAGE 事件）
- **`EventType.USAGE` 事件**（M10.1 加 `usage/duration/estimated` 字段，M10.2 接入落盘）：`transient=False` → 自动落盘（SessionStoreSink）+ 进 `handle.event_buffer`（回放可读）+ `_on_event` 转 `event` 消息（前端经 EVENT payload 读 `usage`）。用量落盘入口：daemon 顶层 `server.py._emit_usage(stream, res, duration, *, parent_message_id)`（替代旧 `transport.report_usage` 的 `MsgType.USAGE` 实时路径，M10.3 删 MsgType.USAGE）；子 agent `SubagentSpawner.spawn(parent_message_id=...)` + `self._emit_subagent_usage(sub_stream, result, duration, parent_message_id=...)`，子事件 `parent_message_id` 指向父 message 形成 message 树。
- **`AgentLoop._run_message_id`**：`run()` 入口设 `= message_id`，`_tool_spawn_subagent` 透传为子 agent 的 `parent_message_id`；**必须在 `__init__` 初始化为 `None`**（否则绕过 run 直接调 spawn 的测试路径 `AttributeError: 'AgentLoop' object has no attribute '_run_message_id'`）。
- **`_emit_subagent_usage(self, stream, result, duration, *, parent_message_id)` 的 `parent_message_id` 是 keyword-only**：调用必须 `parent_message_id=...` 关键字传参，位置传参会 `TypeError: takes 3 positional arguments but 4 were given`。
- 前端归集（M10.4）：桌面端 `useEventReducer` 按 `parent_message_id` 链把子 agent 用量累加回派生子 agent 的那条 message。
- **M10.3：删 `MsgType.USAGE`**：用量从独立 `usage` 消息改为 `event` 消息内的 `USAGE` 事件子类型。契约四处（protocol.py / types.ts / check-msgtype.mjs / docs/daemon-api.md）已同步；`EventTypeStr`/`EVENT_TYPES` 补 `'usage'`。**线格式**：`Event.usage` 仅含 token 字典，`estimated`/`duration`/`message_id`/`parent_message_id` 为 Event 顶层兄弟字段（前端 `AgentEvent` 的 `usage` 为内层字典、`estimated`/`duration` 顶层）。CLI/TUI transport 经 `_on_event` 的 `EventType.USAGE` 分支调 `report_usage` 渲染（与旧 `MsgType.USAGE` 显示等价）；前端只读 `event(USAGE)`，不再有独立 `usage` 消息。
- **M10.4：前端归集**：`useEventReducer` 在 `buildChatModel` 中处理 `'usage'` 事件，累积到 `usageQueue`，USER 事件触发 flush，最终按顺序挂到 `ResponseBlock.turnMeta`。子 agent 段内的 `USAGE` 事件在 main loop `else` 分支提取同样累积到父会话用量（子 agent 块 `SubagentCard` 不渲染 `turnMeta`）。`App.tsx` 不再维护内存版 `turnMeta`/`turnUsageRef`/`turnStartRef`/`estimatedRef`；`MessageList` 不再传 `turnMeta` prop，`ResponseGroup` 直接从 `block.turnMeta` 读取。回放路径与实时路径共享同一 reducer，重启后用量可通过回放 `USAGE` 事件恢复。

### 桌面端（React / TS）
- **reducer 跨轮兜底重复渲染**：`lastDecisionText`（收尾兜底）**不可跨轮残留**。修复用 `hasStreamedText` 守卫：`text` 事件置位并丢弃残留兜底；`flushText` 仅在本轮完全无流式 TEXT 时消费兜底；`user` 事件重置守卫。**判据**：每个 assistant 轮次应有且仅有「一个文本气泡」，无重复。
- **`liveEvent` 守卫**：`replaying || activeId==null` 时不追加事件，避免 replay 期间瞬时事件重复渲染。
- **`upsertTab` 必保留已有 tab 的 `name`**：`attached`/`session_list` 消息常不带 `name`，若用 `id.slice(0,8)` 覆盖会丢失用户命名。
- **HITL `show_plan` 不带 id**：前端必须等 `confirm_plan{id}` 才补 id 使模态可操作，否则无法回传。
- **`js-yaml` v4 用具名导入** `load`/`dump`（无 default 导出，否则 rollup 报 "default is not exported"）；`mergeSettings` 深合并（对象递归、数组整体替换）。
- **daemon 发现日志解析**：正则须用 `[^\s]+` 匹配整条 health URL（旧 `[^\s/]+` 拒绝斜杠导致 `http://.../health` 匹配失败）。
- **契约测试（TS↔Python）**：TS 解析正则须兼容单/双引号（`/["']([^"']+)["']/g`）；Python 端 subprocess 捕获用 `encoding='utf-8'`（node 输出含中文，默认 gbk 会 `UnicodeDecodeError`）。

### Textual TUI（M8，basedpyright 类型检查铁律）
- 不要用 `self._log` 缓存日志容器（`App` 基类已有 `_log` 方法，覆盖会触发 `reportAttributeAccessIssue`）→ 改名 `self._log_container`。
- mixin 的抽象方法必须在基类声明（如 `MessageRenderer` 须声明 `def _mount(self, widget): raise NotImplementedError`）。
- **提交前务必本地跑 `basedpyright`**：CI `fast` 门禁含 `basedpyright`（锁版 `1.39.9`），而本地常只跑 `ruff`/`pytest` 漏掉类型错误，导致「本地过、远端挂」。典型坑：`object`/协议未声明属性就动态赋值（如 `session.daemon_handle=`、`store_factory` 返回 `object` 后 `.list_sessions()`）会触发 `reportAttributeAccessIssue`。协议要动态挂属性就在 `Protocol` 显式声明 `x: Any`；工厂返回类型用具体存储类（`SessionStore`/`TraceStore`）而非 `object`。
- `App.notify` 重写需兼容签名 `def notify(self, message, *args, **kwargs)`（否则 `reportIncompatibleMethodOverride`）。
- `DOMNode.action_toggle` 是保留动作，自定义折叠改名如 `action_toggle_collapse` 并同步 `BINDINGS`。
- `TextArea.action_cursor_up/down` 有 `select: bool = False` 参数，子类重写须带上。
- 子类访问 app 专属方法：`self.app` 被推断为基类 `App`，需 `cast("ChatApp", self.app)`。
- **子 agent 独立块 widget 坑**：`VerticalScroll` 在 `textual.containers`（非 `textual.widgets`）；`Static` 内容用 `.content` 属性（非 `.renderable`），`Syntax` 用 `.code`；`Hit` 展示名在 `.text`（非 `.name`）；`Collapsible` 的 Contents 容器类名 `Collapsible.Contents`。
- TUI `text-style` 合法值**不含 `normal`**（用 `none`，否则 `StylesheetParseError` 整屏崩）。
