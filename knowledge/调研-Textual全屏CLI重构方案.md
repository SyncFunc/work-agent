# CLI 架构级重构调研：Textual 全屏 TUI（对标 Claude Code）

> 调研时间：2026-07-23
> 目的：在「保留旧 CLI、重构完再迁移」前提下，调研如何用 **Textual** 做**架构级全屏重构**，
> 达到 Claude Code 级别的「美观 + 交互方便」。本文是设计调研，不含实现；落地步骤见末尾 §9。
> 前置调研：
> - `调研-agent-cli渲染与runner交互.md`（渲染层与 runner 解耦范式：事件流/发布订阅 + 进程级协议）
> - `调研-CLI美化方案.md`（短期 Rich+ptk 增量清单，与本方案互补）

---

## 1. 结论先行

1. **选型 = Textual**（Rich 同团队出品的声明式 TUI 框架，类 CSS 主题、自带 asyncio 事件循环、
   可跑真终端也可经 `textual serve` 跑浏览器）。它是 Python 界**唯一**成熟且对标
   Claude Code（Ink/React 声明式 + 双缓冲）的全屏方案，且**天然兼容本项目已用 Rich**。
2. **架构不动铁律**：`AgentTransport` 协议 + `EventStream.append/emit` 完全不改；loop / session /
   core 零感知 UI。新 TUI 只是 **新增一个 `AgentTransport` 实现**（`TextualTransport`）+ 一个
   Textual `App`，与旧 `TerminalTransport` 平级。
3. **旧 CLI 保留、并行**：`run` / `chat` / `client`（含 daemon WS 客户端）继续用 `TerminalTransport`，
   一行不改；新加 **`chat --tui`**（或独立 `tui` 命令）走 Textual。验证充分后再把默认 `chat`
   切到 Textual、旧实现标记 `--legacy` 直至删除（迁移期零风险）。
4. **测试不破**：新 `TextualApp` 用 `run_test()` + `Pilot` 做 headless 测试（无需真 TTY）；
   现有 `CliRunner` 单测继续走旧 Transport + `FakeModel`，CI 绿不变。
5. **最难的点 = 事件循环集成**：Textual 自带事件循环，必须把 `Session.step` 作为并发任务跑进去
   （推荐 **thread worker**），并用 `app.call_from_thread` 把事件安全推给 UI、用线程安全
   `Future` + `ModalScreen` 实现 HITL。下文 §4 详述。

> 一句话：**用 Textual 把"滚动聊天"升级为"全屏分区 chat 应用"，但只新增一个传输实现，
> 不碰已验证的 loop/EventStream/daemon 协议。**

---

## 2. 目标体验（对标 Claude Code，逐条可验收）

| 维度 | Claude Code 体感 | 本重构要达到 |
|---|---|---|
| 布局 | 上方可滚动历史 + 底部固定输入 + 顶/底状态栏 | Textual `RichLog`(主区) + `TextArea`(底) + `Header`/`Footer` |
| 流式输出 | 增量 Markdown，不重刷整屏 | `RichLog.write(Markdown(...))` + 节流更新当前消息块 |
| 工具调用 | 可折叠块（Collapsible），参数 JSON 高亮 | `Collapsible` + `Syntax(json)` |
| 写/改文件 | diff 语法高亮预览 | `Syntax(diff,"diff")` 复用现有 `res.diff` |
| 执行中 | spinner + 状态词（thinking / running / stalled） | `LoadingIndicator` / `app.call_later` 动效 |
| HITL | 内联卡片 / 模态确认（y/N、选项） | `ModalScreen` + 按键绑定 |
| 命令发现 | 斜杠命令 + 命令面板 | Textual 内置 `CommandPalette`（Ctrl+P）注册 `/plan` 等 |
| 主题 | 暗/亮 + 多主题 | Textual 内置多主题（textual-dark/light、night、catppuccin-mocha…）+ 自定义 TCSS |
| 鼠标 | 点选/复制/滚动 | Textual 原生鼠标支持 |
| 多端 | 仅终端 | 终端 + 浏览器（`textual serve`），**顺带满足 M7「Web 前端」愿景** |

---

## 3. 为什么是 Textual（及为什么不是别的）

| 候选 | 结论 | 理由 |
|---|---|---|
| **Textual** | ✅ 选 | Rich 同团队；声明式 Widget + TCSS；自带 asyncio loop；`run_test/Pilot` headless 测试；
  内置 `Markdown`/`RichLog`/`Collapsible`/`TextArea`/`CommandPalette`/`ModalScreen`/`Footer`；
  可 `textual serve` 跑浏览器；多主题。与本项目 Rich 栈零摩擦。 |
| 自研 Rich + 手动双缓冲 | ❌ | 等于重造 Claude Code 的 Ink/Yoga/blit，成本极高（见 `调研-agent-cli渲染与runner交互.md`
  §2.1 的 6 阶段管线），且无必要。 |
| PyTermGUI | ❌ | 生态/文档弱于 Textual，与 Rich 兼容性不如 Textual（同团队）。 |
| prompt_toolkit 全屏 | ⚪ 不推荐 | ptk 有 `Application`+布局，但缺声明式组件生态、Markdown/Collapsible 需自造；
  且本项目已踩坑「ptk CheckboxList 在 Rich 占 TTY 下卡死」「Live 与 ptk 不共存」（见
  `调研-CLI美化方案.md` §1、§3.4）。Textual 把这些原生解决了。 |
| Go Lipgloss/Glamour | ❌ | 破坏 Python 单语言栈、增加部署复杂度（旧调研已否）。 |

---

## 4. 架构设计（核心）

### 4.1 分层（与现有契约对齐）

```
                 ┌─────────────────────────────────────────────┐
  用户输入(TextArea)│  Textual App (agent/tui/app.py)            │
   ──提交──▶       │  ├─ Header(模式/ctx%)  Footer(快捷键)       │
                  │  ├─ RichLog 主区: 消息流(User/Assistant/     │
                  │  │            ToolBlock/PermissionCard 部件) │
                  │  └─ TextArea 输入区                          │
                  └───────────────┬─────────────────────────────┘
                                  │ 跑 session.step（run_worker）
                                  ▼
   AgentTransport 协议 ◀──── TextualTransport(agent/runtime/textual_transport.py)
   （新增实现，平级于 TerminalTransport）        │
                                  │ bind(stream) 订阅
                                  ▼
                          EventStream.append/emit  ←──  loop / Session（零改动）
                                  │
                                  ├─ 持久化 sink / trace / 压缩派生（不变）
                                  └─ daemon 回放缓冲（不变，仅收非 transient）
```

- **loop / Session / EventStream / AgentTransport 协议**：全部不动（铁律延续自
  `调研-agent-cli渲染与runner交互.md` §4、§5）。
- **`TextualTransport`**：实现 `AgentTransport` 全部方法（`bind`/`ask`/`approve`/`confirm_plan`/
  `show_*`/`notify`/`report_usage`/`close`），并把 `_on_event(ev)` 暴露为**公开方法**
  （命名同 `TerminalTransport._on_event`），以便 daemon `client` 直接复用（见 §4.6）。
- **Textual App**：只做「把事件变成部件 + 收集输入 + HITL 模态」，不含任何 loop/工具逻辑。

### 4.2 事件循环集成（最难，必须做对）

Textual 的 `App` 自己持有一个 asyncio 事件循环，**不能**在 Textual 应用内再 `asyncio.run()`
（会抛 "cannot be called from a running loop"，本项目 M1.6 已踩过同类坑）。

**推荐方案 B（thread worker，UI 永不卡）**：

1. `chat --tui` 命令：构造 `Session` + `TextualApp`，启动 `app.run()`。
2. App 在用户输入后，把任务 `put` 进一个 `asyncio.Queue`；另起一个 **thread worker**
   （`self.run_worker(self._session_driver, thread=True)`）持有**独立事件循环**
   （在线程内 `new_loop` + `asyncio.run`），循环内 `while True: task=await q.get();
   await session.step(task, transport, ...)` —— 本质是把现有 `_chat_repl` 的 `asyncio.run`
   挪进一个线程，逻辑几乎不变。
3. `TextualTransport` 的 `_on_event` 在**工作线程**收到事件 → 用 `app.call_from_thread`
   把「更新 UI 部件」的回调投递回 Textual 主循环（跨线程安全，官方 API）。
4. **HITL**：`ask`/`approve`/`confirm_plan` 在工作线程被 `session.step` `await`；
   方法内创建一个 `concurrent.futures.Future`，用 `app.call_from_thread` 弹出对应
   `ModalScreen`，用户操作后由屏幕经 `loop.call_soon_threadsafe` 设置该 Future 的结果
   （工作线程的 `await future` 被唤醒）。—— 这是 Textual 官方推荐的"从其它线程更新/交互"模式。

> 备选方案 A（async worker，简单但工具期会卡）：`await self.run_worker(session.step(...))`
> 直接在 App 主循环跑。优点：HITL Future 同 loop、最简单；缺点：**同步工具执行（bash 等）
> 会冻结 UI**，spinner/输入在工具运行期无响应。若接受该限制可先用 A 快速验证，再升级到 B。
> 注意：要工具执行期 spinner 不卡，根本上是把同步工具调度改为 `loop.run_in_executor`
> （tool runtime 改进，可独立做），与 UI 选型正交。

### 4.3 布局与部件

- `Header`：左侧项目名/模式（plan/exec），右侧实时 `ctx: NN%`（从 `context_mgr.estimate_usage()`
  经 `app.call_later` 周期刷新，替代旧 `_status_line` 塞 prompt 前缀的 hack）。
- 主区 `RichLog`（`auto_scroll=True` 吸底）：每条对话 = 一个部件：
  - `UserMessage`（`Static`，左侧竖线/前缀 `›`）；
  - `AssistantMessage`（`Static` 包 `Markdown`，流式时只对"当前消息"重渲染）；
  - `ToolBlock`（`Collapsible`，标题 `🔧 name`，展开显示 `Syntax(json)` 参数 + 结果
    `Syntax(diff)`/`Markdown`；复用现有 `res.diff`）；
  - `PermissionCard`（`Static` 或 `Collapsible`，展示 `Action` + 风险，配 y/N 键）。
- 底部 `TextArea`（多行输入）+ 提交键（`Ctrl+J` / `Enter` 在空行外提交；`Tab` 补全）。
- `Footer`：常驻快捷键提示（`Ctrl+P` 命令面板、`Ctrl+C` 中断、`Esc` 取消输入）。

> 子 agent（`_SubAgentTransport`）：其渲染产出的是 rich `Renderable`（Panel/Group），
> 可直接 `RichLog.write` 进主区（加「▶ subagent: name」前缀），**复用现有累积/裁剪逻辑**；
> 或新写 `_SubAgentTuiTransport` 产出 `ToolBlock` 风格部件。核心不变：子 agent 有独立
> `EventStream`，渲染委派给父传输（与现状一致）。

### 4.4 流式 Markdown（性能）

- 单次 `Markdown` 部件 `update` 会整段重解析（O(n²) 对长输出）。缓解：
  - 仅对"当前正在流式"的消息块重渲染，且**节流**（如每 80–120ms 或每累积 N 字符更新一次），
    用 `app.call_later`；定稿时一次性 `update` 完整内容。
  - 与 Claude Code 的「confirmedRef + LRU token cache」思路一致（见 `调研-agent-cli渲染与runner交互.md` §2.1）。
- 也可用 `RichLog.write` 逐段追加已确认前缀、只对尾段渲染，进一步降 CPU。

### 4.5 主题与命令面板

- Textual 内置多主题（`app.theme = "textual-dark" / "catppuccin-mocha" / "night" …`），
  并支持自定义 TCSS 文件（放 `agent/tui/tui.tcss`）。`settings.ui.theme` 直接映射
  （顺带满足旧 `调研-CLI美化方案.md` §3.1 的主题诉求，且更彻底）。
- 内置 `CommandPalette`（Ctrl+P）：注册命令供 `/plan`/`/exec`/`/skills`/`/compact`/`/context`
  等一键触发，提升"交互方便"；同时保留在 `TextArea` 输入 `/` 走 `dispatch_command` 的旧路径。

### 4.6 与 daemon 客户端的复用

`agent/daemon/client.py` 当前：`transport._on_event(Event.from_dict(p["event"]))` 直调 +
`ask/approve/confirm_plan/show_*/notify/report_usage` + `close()`。
`TextualTransport` 暴露**同名公开方法**（把 `_on_event` 提为 `on_event` 或保留 `_on_event`
且 client 改为 `getattr` 兼容）——这样 `client` 命令未来也能在 TTY 下切 Textual 渲染，
**但重构期 `client` 仍用旧 `TerminalTransport`，不强制改**。

---

## 5. 兼容性证明（不破坏现有铁律）

| 现有契约 / 模块 | 影响 | 说明 |
|---|---|---|
| `AgentTransport` 协议 | ✅ 不变 | `TextualTransport` 只是新实现，签名完全对齐 |
| `EventStream.append/emit` | ✅ 不变 | 订阅式渲染原样复用 |
| `Session.step` / `AgentLoop` | ✅ 不变 | 仅调用处从 `_chat_repl` 挪到 thread worker |
| `core`（loop/session/model） | ✅ 零改动 | 不感知任何 UI |
| `TerminalTransport` / `_SubAgentTransport` | ✅ 保留 | 旧 `run`/`chat`/`client` 继续用 |
| `cli.py` 的 `run`/`chat`/`client` | ✅ 默认不变 | 仅加 `chat --tui`（或新 `tui` 命令） |
| `daemon` 协议 / `BridgeTransport` | ✅ 不变 | 回放缓冲仍只收非 `transient` |
| HITL 回调语义 | ✅ 不变 | 仍是 `await transport.ask(...)`，仅实现改为 ModalScreen |
| 子 agent 渲染委派 | ✅ 不变 | 独立 EventStream + 父传输渲染 |

---

## 6. 测试与 headless 路径（最关键风险缓解）

- **Textual 自带 headless 测试**：`async with app.run_test() as pilot:` + `pilot.click/...
  ` 驱动，**无需真 TTY**，CI 可跑（官方 API，见 `textual.app` 文档）。据此为 `TextualApp`
  写：`run_test` 用例覆盖「提交任务→收到工具块→HITL 模态→确认放行」等。
- **现有 CliRunner 单测**：继续走旧 `TerminalTransport` + `FakeModel`（如
  `tests/unit/test_cli.py`、`test_loop.py` 的 Fake Transport），**完全不碰**，CI 绿不变。
- **`_EventRecordingTransport` / 录制 transport**（测试用）：继续保持非 TTY、非 Textual，
  供单测录制决策序列；Textual 路径不影响它。
- **依赖隔离**：`textual` 放进 `[project.optional-dependencies]` 的 `tui` extra
  （`pip install -e ".[dev,tui]"`），生产核心依赖不强制引入 TUI 栈。

---

## 7. 迁移策略（用户要求：旧 CLI 先保留，重构完再迁移）

- **Phase 0（依赖）**：`pyproject.toml` 加 `textual>=0.80`（或当时稳定版）到 `tui` extra；
  `AgentTransport`/`EventStream` 不动。
- **Phase 1（并行实现）**：新增
  - `agent/runtime/textual_transport.py`（`TextualTransport(AgentTransport)` + `_SubAgentTuiTransport` 可选）；
  - `agent/tui/`（`app.py` + `screens.py`(ModalScreen) + `widgets.py`(消息/工具/卡片) + `tui.tcss`）；
  - `agent/cli.py` 加 `chat --tui`（保留旧 `chat` 默认走 `TerminalTransport`）。
- **Phase 2（验证）**：
  - `pytest` 新增 `tests/unit/test_tui.py`（基于 `run_test`/`Pilot`）；旧 `pytest` 全绿。
  - 真 TTY 手动体验：对标 §2 清单（流式 Markdown、工具折叠块、diff 高亮、HITL 模态、
    Ctrl+P 命令面板、主题切换、鼠标滚动）。
- **Phase 3（迁移）**：把默认 `chat` 切到 Textual（旧 `TerminalTransport` 经 `--legacy`
  保留过渡），运行一段时间稳定后，删除 `TerminalTransport` + `_chat_repl` + `client` 的旧渲染
  （`client` 同步切 Textual 或保持旧）。`run`（非交互/CliRunner）始终走非 TUI 录制/打印路径。

---

## 8. 风险与坑（含已有踩坑映射）

| 风险 | 缓解 |
|---|---|
| 事件循环冲突（Textual 自带 loop，不能内层 `asyncio.run`） | 方案 B：session 跑 thread worker，独立 loop；或方案 A 先验证 |
| 同步工具执行冻结 UI（spinner/输入无响应） | thread worker（B）；或把工具调度改 `run_in_executor`（runtime 改进） |
| Live 与 ptk 不共存（旧踩坑） | 新方案输入用 `TextArea`（在 Textual 内），**彻底不用 ptk 输入**，规避 |
| 流式 Markdown O(n²) | §4.4 节流 + 仅当前块重渲染 |
| Windows 终端乱码/不渲染 | 沿用现有 stdout UTF-8 强转；Textual 原生 Windows 支持更稳 |
| 非 TTY 不能跑 Textual | `run`/CI/CliRunner 永远走旧/录制 transport；Textual 仅真 TTY 命令 |
| 子 agent 面板抢占 | 复用现有 `SubagentPanelHub` 思路，子 agent Renderable 汇入主区 RichLog |
| `client` 直调 `_on_event` 兼容性 | `TextualTransport` 暴露同名公开方法，必要时 `getattr` 兼容 |

---

## 9. 里程碑拆分建议（M8：CLI 全屏重构）

> 按项目「里程碑 + 步骤（方案/验收/知识沉淀）」三要素组织，文件夹 `milestones/M8-CLI全屏重构/`。
> 每步落 `实现方案 / 验收标准 / 知识沉淀`，完成后写 `knowledge/INDEX.md`。

- **M8.0 依赖与骨架**：`pyproject` 加 `tui` extra；`agent/tui/` 空 App 能 `run_test` 通过；
  `chat --tui` 启动即退出不崩。验收：`pytest tests/unit/test_tui.py::test_app_boots`。
- **M8.1 TextualTransport 事件桥接**：实现 `bind` + `_on_event`，把 `TEXT`/`TOOL_USE`/
  `TOOL_RESULT`/`PLAN_PROGRESS`/`DECISION` 映射为 `app.call_from_thread` 更新 RichLog；
  用 `FakeModel` + `run_test` 验证消息/工具块出现。验收：事件→部件断言。
- **M8.2 输入与 chat 循环**：`TextArea` + 提交 → thread worker 跑 `session.step`；
  复用 `dispatch_command` 处理 `/` 命令。验收：提交任务→助理回复出现在主区。
- **M8.3 HITL 模态**：`ask`/`approve`/`confirm_plan` 用 `ModalScreen` + 线程安全 Future；
  覆盖单选/多选/审批/计划确认。验收：`run_test` 模拟按键放行/拒绝。
- **M8.4 流式 + 工具块 + diff 高亮**：`Collapsible` + `Syntax(json/diff)`，节流流式 Markdown。
  验收：长输出不卡、diff 高亮可见。
- **M8.5 主题 + 命令面板 + 状态栏**：TCSS 主题、`CommandPalette` 注册 `/` 命令、`Header` 显 ctx%。
  验收：切换主题、Ctrl+P 触发 `/compact`。
- **M8.6 子 agent 渲染接入 + 验收**：`_SubAgentTuiTransport` 或复用父 RichLog；全量 `pytest`
  仍绿 + 真 TTY 体验对标 §2。验收：`pytest`（含 test_tui）+ 手动体验清单。
- **M8.7 迁移切换（可选，Phase 3）**：默认 `chat` 切 Textual，`--legacy` 保留旧；稳定后删旧。

---

## 10. 参考资料

- Textual 官方文档：`https://textual.textualize.io/`（App API：`call_from_thread`、`run_test`、
  `Pilot`、`workers`；Widgets：`RichLog`/`Markdown`/`Collapsible`/`TextArea`/`CommandPalette`/
  `ModalScreen`/`Footer`/`Header`）
- Textual GitHub：`https://github.com/Textualize/textual`
- PyPI textual：`https://pypi.org/project/textual/`
- 既有调研：`调研-agent-cli渲染与runner交互.md`（解耦范式，铁律来源）、
  `调研-CLI美化方案.md`（短期增量，主题/高亮/补全诉求，本方案一并满足）
- 旧踩坑：M1.6（嵌套事件循环崩溃）、M4.6（rich 标记泄漏 ptk）、M5.4（Live 与 ptk 输入不共存）
- Claude Code UI 范式：`调研-agent-cli渲染与runner交互.md` §2.1（声明式/增量 Markdown/双缓冲），
  本方案用 Textual 等价实现（声明式 Widget + RichLog 流式 + 节流）

---

## 11. 一句话总结

> **用 Textual 新增一个与 `TerminalTransport` 平级的 `AgentTransport` 实现 + 一个全屏 `App`，
> 把"滚动聊天"升级成"分区 chat 应用"（流式 Markdown / 可折叠工具块 / diff 高亮 / HITL 模态 /
> 命令面板 / 多主题 / 鼠标），但 loop、EventStream、daemon 协议一字不改；旧 `run`/`chat`/`client`
> 全程保留，验证充分后再迁移默认。最难处是事件循环集成（thread worker + `call_from_thread` +
> 线程安全 Future），测试用 `run_test`/`Pilot` 保 headless 不破。**
