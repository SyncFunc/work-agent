# CLI 美化方案调研

> 调研时间：2026-07-23
> 目的：在**不改变「渲染层与 runner 解耦」架构**（见 `调研-agent-cli渲染与runner交互.md`）、不泄漏渲染逻辑进 core 的前提下，提升终端观感的具体技术选项与落地建议。
> 结论先行：**无需推倒重来**。当前已用 `rich`（Panel/Markdown/Syntax/Live/Table/Tree）+ `prompt_toolkit`（输入/补全/HITL 选择），观感基础已不错；「美化」应分两条线——① 短期在既有 Rich+ptk 栈内**增量增强**（主题化/统一边框/语法高亮/Spinner/底部工具栏/命令补全）；② 中期若要做**全屏 chat 布局**再评估 Textual（大重写，须保留 headless 路径）。

---

## 1. 现状清点（已具备的"美化"能力）

`agent/runtime/terminal_transport.py`（`TerminalTransport` / `_SubAgentTransport`）已落地：

- ✅ 流式带框 Markdown 面板（`💬 模型输出`，`Markdown`，裁高防刷屏）；
- ✅ 写/改工具 `diff` 用 `Syntax(diff,"diff",theme="ansi_dark")` 高亮（M1.5）；
- ✅ 工具调用/结果/计划/澄清/审批用 `Panel` 分色展示；
- ✅ `Table` 展示 skills / agents；
- ✅ 子 agent 共用顶层唯一 `Live` 面板集（`SubagentPanelHub`，防抢占）；
- ✅ 状态栏 `ctx: NN%` 着色（green/yellow/red，ptk `FormattedText`，见 M4.6 修复）；
- ✅ 思考流 `💭` 暗色增量打印。

**明显可美化缺口**（散落硬编码、无统一风格）：

1. 颜色/边框/图标字符串硬编码（`"green"`/`"cyan"`/`"magenta"`/`ROUNDED` 未统一），换肤要改代码；
2. 工具参数 JSON 用 `f"```{args}```"` 包进 Markdown，**未指定语言 → 无语法高亮**；
3. 长工具（bash/网络）执行期**无 spinner/进度**，静默等待；
4. HITL 选择/审批把状态塞进 prompt 前缀，chat 头略挤；
5. 无"无 emoji 模式"（老旧 Windows 终端 / 纯日志管道可能乱码，已有 `UTF-8` 强转但 emoji 仍可能不显示）；
6. 角色（you / assistant）区分仅靠 emoji 头，无布局分区。

---

## 2. 技术选项对比

| 方案 | 语言/框架 | 适合本项目的程度 | 代价 | 与现有架构兼容 |
|---|---|---|---|---|
| **继续 Rich（增量）** | Python / Rich（已装） | ⭐⭐⭐⭐⭐ 首选 | 低：只改 `TerminalTransport` + 新增 `ui/theme.py` | ✅ 完全兼容；事件订阅渲染不动 |
| **prompt_toolkit 增强** | Python / prompt_toolkit（已装） | ⭐⭐⭐⭐ 输入侧 | 低：底部工具栏 + 补全 | ✅ 仅改输入交互；注意 Live 与 ptk 不共存约束 |
| **Textual** | Python / Textual（Rich 同团队） | ⭐⭐⭐ 全屏重构时 | 高：重写 transport 为 App + worker 集成 asyncio | ⚠️ 需保留 headless 路径（CI/CliRunner 不兼容真 TTY） |
| **PyTermGUI** | Python | ⭐⭐ 备选 | 中 | ⚠️ 生态/成熟度弱于 Textual |
| **questionary** | Python / ptk 封装 | ⭐ 仅 HITL 美化 | 低 | ➖ 已被 ptk 覆盖，价值有限 |
| **Go：Lipgloss+Glamour** | Go（外部子进程） | ❌ 不推荐 | 破坏 Python 单语言栈 | ❌ |

**核心判断**：
- 短期所有"美化"都能在 **Rich + prompt_toolkit** 内完成，且不触碰 `EventStream`/`AgentTransport` 协议 → 最符合"loop 不感知渲染"铁律。
- Textual 是"全屏 chat 应用"的终极方案（真正分区布局、鼠标、CSS 主题、动画），但属于**架构级重构**，应作为独立里程碑（如 M8），且必须保留 `WebTransport`/headless 渲染路径（测试与 daemon 客户端不依赖 TTY）。

---

## 3. 短期增量美化清单（推荐优先做）

### 3.1 统一主题系统（最高性价比）
- 新增 `agent/runtime/ui/theme.py`：集中定义 `rich.theme.Theme`（调色板，可借鉴 Catppuccin Mocha / Gruvbox 配色）+ `BOX`（统一 `box=ROUNDED`）+ `ICONS` 字典（事件/工具 → emoji 或 ASCII）。
- `TerminalTransport` 构造 `Console(theme=THEME)`，所有 `border_style`/`style` 引用主题名而非裸色串。
- **配置代码分离**：`settings.yaml` 增 `ui.theme`（默认 `default`，可选 `catppuccin`/`gruvbox`/`plain`）；`plain` 关 emoji（兼容老终端）。符合项目"配置>代码"约定。

### 3.2 工具参数语法高亮
- `on_tool_call` 当前：`f"[cyan]{name}[/cyan]\n```\n{args}\n```"`（Markdown 代码块无语言 → 无高亮）。
- 改为 `Syntax(args, "json", theme=..., word_wrap=True)` 渲染参数（带 JSON 高亮）；`bash` 工具参数用 `Syntax(cmd,"bash")`；`read` 输出用 `Syntax(out,"plaintext")`。

### 3.3 Spinner / 进度条（长工具期）
- 工具执行（尤其 `bash`/网络）用 `rich.console.Console.status("执行中…")` 或 `rich.progress.Progress` 显示旋转动画，**替代静默等待**。
- ⚠️ 约束：spinner 只在**非 ptk 输入期**（即流式 `Live` 激活期）展示；后台 subagent 期不抢终端（沿用 `slot_budget` 逻辑）。

### 3.4 prompt_toolkit 输入侧增强
- **底部工具栏（bottom toolbar）**：把 `ctx: NN%` + 当前模式（plan/exec）移到 ptk `bottom_toolbar`，不再塞进 prompt 前缀（更干净，且避免 M4.6 那种"rich 标记泄漏"坑）。
- **斜杠命令补全**：输入 `/` 时补全 `plan`/`exec`/`approve`/`mode`/`skills`/`agents`/`skill`/`context`/`compact`/`resume`/`fork`/`agent`/`bg`。
- **输入语法高亮**：命令/参数着色（ptk `lexer` 或 `FormattedText` 输入处理器）。

### 3.5 角色与布局轻量提升
- chat 模式给 `you:` 输入与 `💬 模型输出` 用不同 `border_style`（如用户蓝、助手绿），强化角色区分。
- 可选 `rich.layout.Layout` 把"状态栏 + 对话区"分区（但流式 `Live` 已覆盖主区，收益有限，优先级低于 3.1–3.4）。

### 3.6 无 emoji / 高对比模式
- `ICONS` 字典支持 `plain` 模式（用 `[工具]`/`[计划]` 等 ASCII 文本替代 emoji），照顾 CI 日志与老终端。

---

## 4. 中期：Textual 全屏重构（评估要点）

**何时值得做**：当"滚动式聊天"不再满足（想要固定输入区在底部、上方可滚动历史、鼠标点选、侧边会话列表、多面板并排）。

**落地形态**：
- 新建 `agent/runtime/textual_transport.py`：`TerminalTransport` → Textual `App`；用 `Markdown`/`Static`/`DataTable`/`TextArea` Widget 替代 Panel；`EventStream.subscribe` 在 Textual `Worker` 内消费，经 `call_from_thread` 更新 Widget（Textual 自带 asyncio 事件循环，需把 `Session.step` 放进 `app.run_worker`）。
- HITL（`ask`/`approve`/`confirm_plan`）改为 Textual 模态 `ModalScreen` 或 `Input` + 按键。
- **必须保留 headless 路径**：`WebTransport`（M7 daemon/WS 客户端）与 `CliRunner`/CI 测试走非 Textual 渲染（Textual 要真 TTY，测试不可行）→ 维持 `AgentTransport` 协议下两套实现。

**风险**：测试体系（`CliRunner` + `FakeModel` 驱动 `_EventRecordingTransport`）不兼容 Textual；须保留一个非 TUI 的录制 transport 供单测。代价高，建议作为独立里程碑。

---

## 5. 不推荐项（及理由）

- **PyTermGUI**：生态/文档弱于 Textual，且与 Rich 兼容性不如 Textual（同团队）。
- **questionary**：仅美化 HITL 选择，已被本项目 `_ptk_single_choice`/`_ptk_multi_choice` 覆盖且更稳健（见 M1.6 踩坑⑦：ptk `CheckboxList` 在 rich 已占 TTY 下卡死）。
- **引入 Go 的 Lipgloss/Glamour**：破坏 Python 单语言栈，增加部署复杂度；其"AdaptiveColor + Markdown 富文本"思路可在 Rich 内用 `Theme` + `Markdown` 等价实现。

---

## 6. 落地建议顺序

1. **M-UI.1** 主题系统（`ui/theme.py` + `settings.ui.theme` + 全文件引用主题名/BOX/ICONS）→ 顺带解决"无 emoji 模式"与"颜色散落"。
2. **M-UI.2** 工具参数 `Syntax` 高亮 + 统一 `box=ROUNDED`。
3. **M-UI.3** 长工具 Spinner（`Console.status`）。
4. **M-UI.4** ptk 底部工具栏 + 斜杠命令补全。
5. （可选）**M8** Textual 全屏重构，保留 headless 路径。

> 以上均**不修改** `EventStream` / `AgentTransport` / core 循环——新增渲染只走事件订阅，保持"loop 不感知渲染"铁律（见 `调研-agent-cli渲染与runner交互.md` §4 已有铁律）。

---

## 7. 参考资料

- Rich 文档（Theme / Syntax / Progress / Layout / Console.status）：https://rich.readthedocs.io/
- Textual 文档（Widgets / Markdown / App / Worker）：https://textual.textualize.io/
- prompt_toolkit 文档（PromptSession / bottom_toolbar / autocompletion）：https://python-prompt-toolkit.readthedocs.io/
- 既有调研：`调研-agent-cli渲染与runner交互.md`（渲染/runner 解耦范式）
- 既有踩坑：M1.6 踩坑⑦（ptk CheckboxList 卡死）、M4.6（rich 标记泄漏到 ptk）、M5.4（rich Live 与 ptk 输入行不共存）
