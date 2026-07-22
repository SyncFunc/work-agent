# 业界 Agent CLI 渲染方案与 Runner 交互调研

> 调研时间：2026-07-22
> 目的：为 `M6-生产化` / `M7-agentrunner守护进程分离` 的「渲染层与 runner 解耦」提供业界参照。
> 结论先行：**主流 Agent CLI 都已放弃"打印文本"，转向「事件驱动 + 声明式渲染」**；渲染层与 runner（agent loop）之间通过 **事件流/发布订阅** 或 **进程级 client-server 协议** 解耦，runner 只生产事件、绝不感知 UI。本项目既有 `AgentTransport` + `EventStream.subscribe/emit` + `M7 daemon/WS` 架构与这一范式完全一致，无需推倒重来，只需在「渲染质量」与「多后端复用」上做增量增强。

---

## 1. 渲染方案技术栈对比

| 工具 | 语言/框架 | 渲染范式 | 与 runner 解耦方式 | 特点 |
|---|---|---|---|---|
| **Claude Code** | TypeScript / **React + Ink**（自研 40+ 文件，Yoga 布局 + 双缓冲） | 声明式组件树（140+ 组件） | AppState(Zustand) 单一真实来源；loop 写 state，UI 订阅细粒度重渲染 | 工业级；增量 Markdown、虚拟滚动、双缓冲 blit |
| **OpenAI Codex CLI** | Rust（`codex-rs` workspace） | Rust TUI | core agent 输出 **event stream**，tool 执行器作为 runner 消费；CLI/TUI 多端入口共用协议 | 多入口（CLI / app / MCP server）共用同一执行安全逻辑 |
| **Gemini CLI** | TypeScript | 简单格式化（不依赖重型 TUI 框架） | `packages/cli`（渲染）↔ `packages/core`（编排）分层；事件序列回传 | 模块化清晰，渲染职责全在 cli 包 |
| **OpenCode** | Go / **Bubble Tea**（Elm 架构）+ Lipgloss + Glamour | 函数式 Model-Update-View | **pubsub 事件总线** `pubsub.Event[agent.AgentEvent]`；Agent 推事件 → TUI `Update` 循环 | AdaptiveColor 主题、Markdown 富文本 |
| **Aider** | Python | 轻量、透明（以 diff 为核心） | repo map 压缩上下文；LLM 产出 search/replace diff，本地应用 | 反"花哨 UI"路线，强调可审阅的 diff |
| **ACP**（标准协议） | 语言无关 | 由 Client（IDE/编辑器）决定 | JSON-RPC 2.0：Agent 作为 Client 子进程，`session/update` 通知推送进度 | 标准化解耦契约，Zed/JetBrains 推动 |

---

## 2. 各方案详解

### 2.1 Claude Code —— React + Ink 声明式渲染（标杆）

**渲染管线（6 阶段）**：JSX → React VDOM(react-reconciler) → Ink 布局(Yoga 算字符格) → Output 操作(含 `blit` 块拷贝未变区域) → Screen 缓冲(CharPool 整数索引) → 双缓冲 Diff(只发最小 ANSI) → `stdout.write`(帧率节流)。

**关键设计**：
- **组件树分层**：基础层 `Box/Text/ScrollBox`(Yoga Flexbox 子集) → 应用层 `Message` 按类型路由(`UserText/AssistantText/ToolUse/ToolResult`)、`PermissionRequest`、`FileEditToolDiff`、`Spinner` 等，共 140+ 组件。
- **增量 Markdown 渲染**：`StreamingMarkdown` 用 `confirmedRef` 缓存已确认前缀，仅对新到达尾段做 `marked.lexer`，复杂度从 `O(总长)` 降到 `O(新增)`；LRU token 缓存(上限 500)跨帧复用；前 500 字符正则快速路径跳过纯文本渲染。
- **性能优化**：`useMemoCache` 仅重建 prop 变化的节点(约 10x 省)；`blit` 未变区直接拷上一帧；CharPool 整数比较；`useVirtualScroll`(Overscan 80 行、每帧最多挂 25 项)避免长会话崩溃。
- **Spinner/状态**：内置 200+ 随机动词、shimmer 渐变、stalled 检测(API 无响应转红)、多维信息融合。

**与 runner 解耦**：`AppStateProvider`(Zustand) 为根 Provider；Agent Loop 写入对话/工具/任务状态，UI 经 `useAppState` 订阅触发细粒度重渲染。多 Agent 各自独立组件，互不覆盖。

### 2.2 OpenAI Codex CLI —— Rust TUI + 多端复用

- **`codex-rs` Rust workspace**：`cli`/`tui`(terminal)、`app-server`(desktop)、`mcp-server`(暴露为工具) 共用 `core`/`core-api`(session/event protocol/loop) 与 `shell-command`(安全分类)。
- **解耦点**：core agent 与模型后端交互产出 **event stream**；`shell`/`apply_patch`/`MCP` 执行器作为 runner 消费事件；每次工具执行前经 **approval/sandbox 治理层**做策略状态判定（自动通过 / 触发审批 / 沙箱隔离）——本质是基于策略的状态机。MCP 路径中 approval request → elicitation → 用户响应回灌 session，体现**中断-恢复**流转。

### 2.3 Gemini CLI —— 分层包 + 轻量渲染

- **`packages/cli`(前端) ↔ `packages/core`(后端)**：cli 管用户输入、历史、`Display rendering`、`Theme`；core 收请求、编排 Gemini API、管工具与会话。二者分离、可独立开发与替换前端。
- **交互流**：用户输入 → cli 发 core → core 构建 prompt+工具定义 → API 返回答案/工具调用 → 涉及文件/Shell 修改时 core **先向用户展示工具与参数、需批准** → 结果回传 API → 最终响应回 cli 格式化显示。

### 2.4 OpenCode —— Go + Bubble Tea (Elm 架构)

- **Model-Update-View**：`appModel` 持有 width/height、页面路由 `pages map[PageID]tea.Model`、状态栏；`Update(msg tea.Msg) (tea.Model, tea.Cmd)` 集中处理 `WindowSizeMsg`/`KeyMsg`/`pubsub.Event[agent.AgentEvent]`。
- **事件总线解耦**：Agent 后端把流式/完成/错误封装为 `pubsub.Event[agent.AgentEvent]` 发往 TUI 的 `Update` 循环；错误经 `util.ReportError` 以状态栏 `InfoMsg` 传递。
- **渲染生态**：Lipgloss(样式 + `AdaptiveColor` 自动适配明暗) + Glamour(`glamour.Render` Markdown 富文本) + 10+ 内置主题(Catppuccin/Gruvbox…)；对话框用 Lipgloss 绘制，图标常量增强可视化。

### 2.5 Aider —— 轻量透明（反范式参照）

- 不同路线：不追求富 TUI，核心是 **repo map**（把仓库结构压缩进上下文）与 **search/replace diff 应用**。LLM 产出 diff 片段，本地确定性应用并展示 diff 供审阅。
- 价值：证明"好用的 Agent CLI"不一定需要重型框架，**渲染的透明度（可读 diff）与上下文效率**有时比动画更重要。适合作为"极简渲染"对照。

### 2.6 Agent Client Protocol (ACP) —— 标准化解耦契约

- **JSON-RPC 2.0**：两类消息 —— Method(请求-响应，带 id) / Notification(单向，无回执)。
- **生命周期**：`initialize` → `session/new` → 多轮 `session/prompt`；Agent 持续发 `session/update`(消息块/工具调用/计划/模式变更) 通知进度；Client 用 `session/request_permission` 向用户请求授权；`session/cancel` 中断单轮。
- **意义**：把"渲染层(Client) ↔ runner(Agent)"的边界标准化为协议，使同一 Agent 可插到任意 IDE/终端。本项目 M7 的 WS 协议与其同构（事件转发 + HITL 请求/应答闭环）。

---

## 3. 渲染层与 Runner 的交互模型（核心结论）

业界存在两条成熟解耦主线，本项目**两条都已具备**。

### 3.1 主线 A：事件流 / 发布订阅（同一进程内）

> Runner = 事件生产者；渲染层 = 订阅者。二者只通过"事件"耦合。

```
Agent Loop ──append/emit──▶ EventStream ──subscribe──▶ Renderer(UI)
                                  │
                                  └──▶ 持久化 sink / 审计 / 压缩派生
```

- **Claude Code**：AppState(Zustand) 作单一真实来源；loop 写 state，UI 订阅。
- **OpenCode**：`pubsub.Event[agent.AgentEvent]` 总线；Agent 推事件 → TUI Update。
- **本项目**：`EventStream.subscribe(sink)` + `append`(持久化) / `emit`(瞬时、不入档，如 `tool_call_delta` 预览) → `TerminalTransport.bind(stream)`，`_on_event` 按 `type` 映射渲染。**已等价于业界范式**，且 `transient` 字段分离瞬时事件的设计与 Claude Code 思路一致。

### 3.2 主线 B：进程级 Client-Server（runner 在子进程/daemon）

> runner 与渲染层不在同一进程，靠传输层协议通信。好处：渲染崩溃不影响执行、可远程、可多前端。

```
[前端 CLI/Web] ──WS/JSON-RPC──▶ [daemon/子进程 runner] ──▶ core loop ──▶ 工具执行器
      ▲                              │  event 转发 / HITL 请求-应答
      └────────── session/update ───┘
```

- **Codex**：core 可跑成 app-server/daemon，多端共用协议。
- **Gemini CLI**：`packages/cli` ↔ `packages/core` 同一仓库内分层。
- **ACP**：Agent 作为 Client 子进程运行。
- **本项目 M7（已落地）**：daemon(`SessionRegistry`+`BridgeTransport`+WS/HTTP) 常驻；前端仅渲染+输入；事件复用 `Event.to_dict()/from_dict()` 经 `event` 消息转发；HITL 由 `BridgeTransport` 封装为带 `id` 请求、`await Future`，客户端回传同 `id` 应答唤醒。session 切换=detach+attach，回放缓冲(`deque(maxlen=200)`，仅收非 `transient` 事件) 支撑无缝切换。

### 3.3 统一输出漏斗（Output Funnel）

- **Claude Code**：`cli/print.ts` 作输出漏斗，统一格式化并支持多后端(`stdout`/HTTP/文件/NDJSON)。Agent 核心不绑定实体终端，可无缝切远程/日志模式。
- **本项目对应**：`AgentTransport` 协议即渲染抽象；M7 已让"Web 前端只需再实现 `WebTransport` 订阅事件转发 WS"——这正是输出漏斗的多后端思想。

### 3.4 输入交互（HITL）与中断-恢复

- 权限/澄清/计划确认是交互高频点，业界统一做法：**runner 不感知 UI**，把"请求授权"作为回调/请求消息抛出，由 transport 层承接（Claude Code PermissionCard、Codex elicitation、ACP `session/request_permission`）。
- 多 Agent 并行时，声明式/组件化渲染天然隔离（Claude Code 每 agent 独立组件 + 彩虹色；本项目 `TerminalTransport` 复用 + M5 子 agent 摘要返回）。

---

## 4. 关键设计要点归纳（可复用清单）

1. **声明式 > 命令式**：避免 `moveCursor/clearLine` 这类原生命令式重绘；状态驱动重渲染（React/Ink、Elm/Bubble Tea、本项目 EventStream→bind 皆同）。
2. **流式渲染三段优化**（防刷屏/防卡）：
   - 增量 Markdown（只解析新尾段，confirmedRef/LRU token cache）；
   - 双缓冲 + blit（只发变化帧，项目已有 `rich.live.Live` 段裁高方案）；
   - 虚拟滚动（长会话防崩）。
3. **瞬时 vs 持久事件分离**：流式参数预览 `tool_call_delta` 只实时转发、不进持久化/回放缓冲（本项目 `transient=True` 已落地，与 Claude Code/Codex 一致）。
4. **多输出后端**：TTY / 文件 / HTTP / NDJSON 共用同一事件源（漏斗思想）。
5. **HITL 回调完全解耦**：loop 只发"需要授权"信号，具体弹窗/应答在 transport 层。
6. **轮次间可切换模式**（plan/exec）由会话层持有、每次 `run` 传参，loop 无状态（本项目 `Session.plan_mode` 入参覆盖，已落地）。

---

## 5. 对本项目的启示与映射

> 既有架构已对齐业界范式，下面是**增量增强**建议，而非重构。

| 业界做法 | 本项目现状 | 可借鉴增强 |
|---|---|---|
| Claude Code 增量 Markdown(LRU token cache + 快速路径) | `TerminalTransport` 用 `Live`+段裁高，已防刷屏 | 给流式内容面板加 token 缓存 / 纯文本快速路径，进一步降 CPU |
| Codex/ACP 进程级解耦 | M7 daemon+WS 已落地 | Web 前端只需新增 `WebTransport`（`bind` 里 `stream.subscribe(ws.send)`），无需改 loop |
| OpenCode pubsub / Claude AppState | `EventStream.subscribe/emit` 已等价 | 已满足；保持"新增实时渲染走事件，不再给 loop 加 presenter 回调"铁律 |
| 多 Agent 彩虹色隔离 | M5 子 agent 摘要返回 + `TerminalTransport` 复用 | 长会话可加虚拟滚动 / 状态栏上下文占比(已有 `/context`) |
| Aider 透明 diff | `edit`/`write` 已回传 `diff` 并用 `Syntax("diff")` 渲染 | 保持"diff 优先于纯字符数"的展示原则 |

**已有铁律（务必保持）**：
- loop 不感知渲染：`presenter` 回调已废弃，统一走 `EventStream` 事件（`append` 持久 / `emit` 瞬时）。
- daemon 回放缓冲只收非 `transient` 事件，避免 `tool_call_delta` 参数预览重复渲染。
- `Event.to_dict/from_dict` 直接经 WS 转发，HITL 经带 `id` 请求/应答闭环。

**已踩过的坑（与业界一致，勿复现）**：
- 面板高于终端可视高度时 `Live` 整块重打印造成刷屏（本项目 M1.6 已修复：每段独立 `_buf`、`start()/stop()` 不跨段累积、流式裁高、段末定稿一次）。
- 渲染层绝不能泄漏进 core：core 不构造 `TerminalTransport`，后台子 agent 通知经 `transport.notify` 缓冲、安全时机刷出（M5.4 已落地）。

---

## 6. 参考资料

- Claude Code Terminal UI（官方 agentic-design）：声明式渲染、Zustand + Ink + `cli/print.ts` 漏斗
- Claude Code Deep Dive 第 18 章：Ink/React 渲染管线、140+ 组件、增量 Markdown
- claude-harness.dev Terminal UI：自研 Ink、Yoga+双缓冲+CharPool+虚拟滚动
- OpenAI Codex CLI 架构分析（codex-rs workspace / event stream / approval-sandbox 治理）
- Gemini CLI Architecture：`packages/cli` ↔ `packages/core` 分层
- OpenCode TUI：Bubble Tea (Elm) + pubsub 事件总线 + Lipgloss + Glamour
- Agent Client Protocol (ACP)：JSON-RPC 2.0、Method/Notification、`session/update` 事件流

---

## 7. 一句话总结

> **渲染层与 runner 的解耦，业界共识是"事件流/发布订阅"或"进程级 client-server 协议"，runner 只生产事件、绝不感知 UI。** 本项目 `AgentTransport` + `EventStream` + M7 daemon/WS 已落成对同样范式，后续重点是渲染质量增量增强（增量 Markdown、多后端 WebTransport）与保持"loop 不感知渲染"的边界。
