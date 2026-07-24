# Claude Code：Subagents 与 Skills 机制调研（M5 设计依据）

> 来源：Claude Code 官方文档（sub-agents / skills）+ 本项目代码库现状（code-explorer 子 agent 调研）。
> 用途：M5「扩展能力」里程碑的设计依据。M5 交付 `SkillLoader` + `SubagentSpawner`。
> 配套：本项目「能力正交」约定（见 `knowledge/INDEX.md` 架构决策）——`Tool`(原子) / `Skill`(按需包) / `Subagent`(隔离上下文) 三层。
> 关联：M4 已完成上下文四层压缩防线，`claude-code-context-management.md` 是其依据；本文件是 M5 的对应依据。

---

## 1. 为什么研究 Claude 的方案

M5 要解决两件事：

1. **Skill（按需包）**：把「一段可复用的提示词 + 脚本 + 参考文档」打包成可触发的能力，模型按需加载，正文不常驻上下文（省 token）。
2. **Subagent（隔离上下文）**：把子任务委派给一个**独立上下文窗口**的分身，主上下文只拿回摘要，突破上下文瓶颈（M4 已解决「压缩」，M5 解决「分叉」）。

Claude Code 在这两件事上的设计成熟、字段明确，且天然契合本项目「98/1.6 法则 / 能力正交 / 上下文稀缺 / 两条全局主线（事件流 + trace）」的架构决策。下面先把 Claude 的方案讲清楚，再逐条映射到本项目既有接口。

---

## 2. Claude Code Subagents 机制

### 2.1 定义文件格式（`.md` + YAML frontmatter）

Subagent 是一个**带 YAML frontmatter 的 Markdown 文件**。frontmatter 存元数据，正文成为指导 subagent 行为的**系统提示**（只接收此系统提示 + 基本环境，而非主会话完整系统提示）。

| 字段 | 必需 | 说明 |
|---|---|---|
| `name` | 是 | 小写字母 + 连字符的唯一标识；Hooks 以 `agent_type` 接收 |
| `description` | 是 | 模型决定何时委派的任务描述（建议含 "use proactively" 鼓励主动委派） |
| `tools` | 否 | 允许的工具列表；省略则继承所有。支持 `Agent(agent_type)` 语法限制可生成的子类型 |
| `disallowedTools` | 否 | 拒绝列表，从继承/指定列表移除（支持 `mcp__<server>` 模式） |
| `model` | 否 | `sonnet`/`opus`/`haiku`/`fable`、完整模型 ID、或 `inherit`（默认）；`CLAUDE_CODE_SUBAGENT_MODEL` 可覆盖 |
| `permissionMode` | 否 | `default`/`acceptEdits`/`auto`/`dontAsk`/`bypassPermissions`/`plan`/`manual` |
| `maxTurns` | 否 | 停止前的最大代理轮数 |
| `skills` | 否 | 启动时**预注入完整技能内容**（非仅描述） |
| `mcpServers` | 否 | 内联定义或引用已配置 MCP 服务器 |
| `hooks` | 否 | 限定于该 subagent 生命周期的 hooks |
| `memory` | 否 | 持久内存范围：`user`/`project`/`local` |
| `background` | 否 | `true` 则始终后台运行 |
| `effort` | 否 | `low`/`medium`/`high`/`xhigh`/`max`，覆盖会话级 |
| `isolation` | 否 | `worktree` 在临时 git worktree 中运行，提供隔离副本 |
| `color` | 否 | 显示颜色 |
| `initialPrompt` | 否 | 主会话代理运行时自动提交的首个用户轮次 |

**工具控制逻辑**：若 `tools` 与 `disallowedTools` 同时设置，`disallowedTools` 先应用，再对剩余池解析 `tools`。部分工具（如 `AskUserQuestion`、`EnterPlanMode`）对 subagent 无效，即使列出也忽略。

### 2.2 文件存放位置与优先级（同名高覆盖低）

| 位置 | 范围 | 优先级 |
|---|---|---|
| 托管设置目录 `.claude/agents/` | 组织 | 1（最高） |
| `--agents` CLI 标志（JSON） | 当前会话 | 2（不存盘） |
| `.claude/agents/` | 当前项目 | 3（建议检入版本控制） |
| `~/.claude/agents/` | 所有项目 | 4（用户级，递归扫描） |
| Plugin `agents/` | 启用 plugin 处 | 5（最低，子目录成 scoped id） |

实时检测：监视 `~/.claude/agents/` 与 `.claude/agents/`，增删改数秒生效（无需重启），但**新建 agents 目录的首个文件**需重启。

### 2.3 内置 Subagent 类型

| 类型 | 模型 | 工具 | 特性 |
|---|---|---|---|
| **Explore** | 继承主对话 | 只读（拒 Write/Edit） | 快速代码库搜索；**跳过 CLAUDE.md 和 git 状态** |
| **Plan** | 继承主对话 | 只读（拒 Write/Edit） | plan mode 期间研究；**跳过 CLAUDE.md 和 git 状态** |
| **General-purpose** | 继承主对话 | 所有工具 | 复杂多步骤；加载 CLAUDE.md 和 git |

禁用内置：`permissions.deny` 加 `Agent(Explore)`，或 `CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS=1`。

### 2.4 上下文隔离方式（关键）

- **独立上下文窗口**：每个 subagent 在自己的 context window 运行；主对话历史、已调技能、已读文件**均不可见**（fork 模式除外）。
- **非 fork subagent 启动加载内容**：① 系统提示（自身定义 + 环境详情）② 任务消息（Claude 编写的委托提示）③ CLAUDE.md 与内存层次（Explore/Plan **跳过**）④ Git 状态快照（Explore/Plan **跳过**）⑤ 预加载技能（skills 字段）⑥ 兄弟名单（v2.1.206+，仅 SendMessage 且有其他命名代理时）。
- **Fork 模式**：继承整个对话（系统提示/工具/模型/消息历史），共享 prompt cache，但自身工具调用仍在主上下文之外。Fork 无法再生成 fork，但可生成其他类型（计入深度）。

### 2.5 调用方式

1. **自动委托**：模型据 `description` + 上下文自动匹配。
2. **自然语言**：提示中命名（如 "Use the test-runner subagent to…"）。
3. **@-mention**：`@"code-reviewer (agent)"` 保证该类型运行；`@agent-<name>` 或 `@agent-my-plugin:code-reviewer`。
4. **会话范围**：`claude --agent code-reviewer` 使主线程采用该配置；或 settings 设 `"agent"` 字段。
5. **恢复**：`SendMessage` 工具传 ID/名称恢复（Explore/Plan 一次性不可恢复）。

### 2.6 并行与嵌套

- **并行研究**：生成多个 subagent 独立探索，结果回主对话后综合。
- **前后台**：默认后台；前台阻塞至完成。后台权限提示在主会话显示。
- **嵌套**：subagent 可生成自己的 subagent，**深度限制固定为 5**（深度 5 不接收 Agent 工具）。后台恢复不改深度。防嵌套：从 `tools` 省略 `Agent`。

### 2.7 返回结果机制（关键）

- **摘要返回**：详细输出（搜索结果/日志/文件内容）保留在 subagent 上下文，**仅相关摘要/结果返回主对话**，节省主上下文。
- **前台/后台差异**：前台阻塞并传递权限提示；后台并发，完成时详情视图保持打开，`/tasks` 列出。
- **API 错误处理**：前台被切断返回部分输出 + 注明未完成；后台标记失败并附最后输出。
- **转录持久化**：每次调用存为 `agent-{agentId}.jsonl`（独立于主对话，主对话压缩不影响），按 `cleanupPeriodDays`（默认 30 天）清理。

---

## 3. Claude Code Skills 机制

### 3.1 SKILL.md 格式与 frontmatter

Skill 是**目录**，至少有 `SKILL.md`（含 YAML frontmatter + Markdown 正文）。**所有字段均可选**，但推荐 `description`。主文件**少于 500 行**，详细内容挪到独立文件。

| 字段 | 必需 | 说明 |
|---|---|---|
| `name` | 否 | 显示名，默认取目录名（插件根 SKILL.md 决定命令名） |
| `description` | 推荐 | 模型决定何时自动应用；省略则取正文第一段。`description`+`when_to_use` 截断上限 1,536 字符 |
| `when_to_use` | 否 | 额外触发上下文（短语/示例请求），附到列表 description，占用 1,536 上限 |
| `argument-hint` | 否 | 自动完成提示，如 `[issue-number]` |
| `arguments` | 否 | 命名位置参数（空格分隔串或 YAML 列表），按顺序映射 `$name` 替换 |
| `disable-model-invocation` | 否 | `true` 阻止自动加载（仅 `/name` 手动调用），也防预加载至 subagents。默认 `false` |
| `user-invocable` | 否 | `false` 从 `/` 菜单隐藏（背景知识，仅 Claude 调）。默认 `true` |
| `allowed-tools` | 否 | skill 活动时 Claude 可免批准使用的工具（如 `Bash(git add *)`） |
| `disallowed-tools` | 否 | 活动时从工具池移除的工具，下条消息发送后清除 |
| `model` | 否 | 活动时使用的模型，覆盖当前轮（接受与 `/model` 同值或 `inherit`） |
| `effort` | 否 | 活动时工作量：`low`/`medium`/`high`/`xhigh`/`max` |
| `context` | 否 | 设 `fork` 则在**分叉 subagent 上下文**运行（隔离，无对话历史） |
| `agent` | 否 | `context: fork` 时使用的 subagent 类型（Explore/Plan/general-purpose/自定义） |
| `hooks` | 否 | 限定于该 skill 生命周期的 hooks |
| `paths` | 否 | Glob 模式，限制仅在处理匹配文件时自动加载（逗号分隔/YAML 列表） |
| `shell` | 否 | `!command` 及 ```` ```! ```` 块用的 shell：`bash`/`powershell` |

### 3.2 文件存放位置与发现

| 位置 | 范围 |
|---|---|
| 企业（托管设置） | 组织所有用户（最高） |
| `~/.claude/skills/<skill-name>/SKILL.md` | 用户级（个人所有项目） |
| `.claude/skills/<skill-name>/SKILL.md` | 项目级（仅当前项目，建议检入） |
| `<plugin>/skills/<skill-name>/SKILL.md` | 插件（命名空间 `plugin-name:skill-name`） |

优先级：企业 > 个人 > 项目；同名高级别覆盖低级别。自动发现：从起始目录到仓库根的每个父目录 `.claude/skills/` 均加载（monorepo 嵌套 skill 以 `apps/web:deploy` 限定名出现）；`--add-dir` 目录自动加载。**实时变更检测**：会话中增删改立即生效（仅 SKILL.md 文本；插件类需 `/reload-plugins`）。

### 3.3 触发与加载机制

- **描述常驻上下文**：默认 skill 的 `description` 始终载入上下文供模型匹配；**完整 SKILL.md 正文仅在调用时加载**，降低 token 成本。
- **触发判断**：模型据 `description` + `when_to_use` 决定相关性；`disable-model-invocation: true` 则从上下文移除，仅 `/name` 调用。
- **`paths` 限制**：设 Glob 后仅当处理匹配文件才自动加载。
- **动态上下文注入**：`!command` 语法在发送给模型前执行 shell 命令，输出替换占位符（预处理，模型仅见结果）。

### 3.4 作为工具调用的底层机制

- **工具包注册**：创建 `SKILL.md` 后加入工具包；可 `/skill-name` 直接调用，或由模型自动加载。
- **权限规则**：`Skill`（拒绝全部）/`Skill(commit)`（精确）/`Skill(deploy *)`（前缀带参）；`allowed-tools` 在 skill 活动时授予免批工具。
- **内容生命周期**：调用时 SKILL.md 作为**单个消息**进入对话，会话余下部分保持；不重新读取文件。自动压缩时前 5,000 token/每个、组合 25,000 token 预算内转发。
- **字符串替换机制**（skill 内容中）：
  - `$ARGUMENTS`：全部参数（不存在则追加 `ARGUMENTS: <value>`）
  - `$ARGUMENTS[N]` 或 `$N`：0 基索引参数
  - `$name`：声明于 `arguments` 的命名参数
  - `${CLAUDE_SESSION_ID}` / `${CLAUDE_EFFORT}` / `${CLAUDE_SKILL_DIR}`（含脚本引用）/ `${CLAUDE_PROJECT_DIR}`
  - 转义：`\\$1.00` 保留文字 `$`；多词值用引号包裹
- **堆叠调用**：`/code-review /fix-issue 123` 加载多个 skill，尾部文本作为 `$ARGUMENTS` 传各 skill（最多扩展 1+5 个）。

### 3.5 多文件 Skill 包

```
my-skill/
├── SKILL.md           # 主要说明（必需）
├── template.md        # Claude 填写的模板
├── examples/
│   └── sample.md      # 示例输出
└── scripts/
    └── validate.sh    # Claude 可执行脚本
```

- 脚本执行用 `${CLAUDE_SKILL_DIR}` 解析路径（不受安装级别影响）。文档示例 `codebase-visualizer` 用 Python 生成 HTML 可视化，经 `allowed-tools: Bash(python3 *)` 授权运行。
- 内置 `/doctor`、`/code-review`、`/batch`、`/debug`、`/loop`、`/claude-api` 等基于提示，可用 `disableBundledSkills` 禁用。

### 3.6 Skill 与 Subagent 的关系

`context: fork` + `agent: Explore` 使 skill 内容变 subagent 提示，**隔离执行**（Explore/Plan 跳过 CLAUDE.md）。这把「Skill（知识包）」与「Subagent（隔离上下文）」打通——Skill 可声明自己在 fork 出的 subagent 里跑。

---

## 4. 本项目现状（M5 集成点清单）

> 来源：code-explorer 子 agent 调研。结论：**M5 是全新模块，无任何既有实现**（`agent/subagent.py`、`agent/skills/` 均不存在），但可复用以下全部既有接口。

### 4.1 AgentLoop（无状态，天然支持上下文隔离）

`agent/core/loop.py`：

```python
class AgentLoop:
    def __init__(
        self,
        model: Model,
        registry: ToolRegistry,
        settings: Settings,
        tracer: Tracer | None = None,
        plan_mode: bool | None = None,
        plan_path: str | None = None,
        sandbox: "Executor | None" = None,
        gate: "ApprovalGate | None" = None,
    ) -> None: ...

    async def run(
        self,
        task: str,
        messages: list[Message] | None = None,
        *,
        clarify_total: int = 0,
        plan_mode: bool | None = None,
        plan_path: str | None = None,
        transport: "AgentTransport | None" = None,
    ) -> AgentResult: ...


@dataclass
class AgentResult:
    text: str
    events: EventStream
    iterations: int
    needs_clarification: bool = False
    questions: list[Question] | None = None
    messages: list[Message] | None = None  # 更新后的对话历史（不含 system）
    clarify_total: int = 0
    plan: str | None = None
    plan_path: str | None = None
    plan_steps: list[PlanStep] | None = None
    needs_plan_confirm: bool = False
    usage: dict[str, int] = field(default_factory=dict)
    soft_limit_hit: bool = False
```

**关键约束**：
- **loop 实例无状态**：不保存对话历史。`run(task, messages)` 接收历史 → 返回 `AgentResult.messages`。子 agent 复用主循环**只需传入独立的 `messages` 列表**即可隔离上下文。
- `run` 入口调 `Tracer.reset_current_span()`（行 118）→ 重置隐式 parent span，是父子 span 隔离关键。
- 子 agent 可注入**不同的 `model`/`registry`/`sandbox`/`gate`**（构造器参数），实现模型降级、工具白名单、独立沙箱。

### 4.2 ContextManager（每个 subagent 需独立实例）

`agent/context/manager.py` + `agent/context/__init__.py`：

```python
class ContextManager:
    def __init__(
        self,
        context_window=200_000,
        max_output_tokens=20_000,
        compact_buffer=13_000,
        *,
        system_fixed_tokens=3_000,
        system_dynamic_tokens=0,
        tools_tokens=15_000,
        microcompact_keep_recent=5,
        microcompact=None,
        auto_compact=None,
        tracer=None,
    ): ...
    def set_conv(self, conv: list[Message]) -> None: ...
    def get_active_messages(self) -> list[Message]: ...
    def mark_boundary(self) -> None: ...
    async def apply_microcompact(self) -> list[Message]: ...
    async def compact(self) -> bool: ...
```

**约束**：`ContextManager` 自身持有 `self.conv`；压缩只作用于 conv 投影，**绝不碰 `EventStream`**（审计真相不可变，M4.1 铁律）。子 agent 应拥有**独立的 `ContextManager` 实例**（独立 conv/边界/历史）。`compact()` 的 `_anti_drift()` 会重读最近文件——子 agent 复用需注意 `recent_files` 不与父 agent 串。

### 4.3 AgentTransport / EventStream（子 agent 事件流隔离）

`agent/core/transport.py`（`AgentTransport` Protocol）+ `agent/core/events.py`：

- `AgentTransport`：`interactive` / `ask` / `show_questions` / `show_plan` / `confirm_plan` / `approve` / `notify` / `bind(stream)` / `close()` / `report_usage(usage, answer)`。
- `EventStream.subscribe`/`unsubscribe`/`append`/`emit`；`Event.type` 含 `decision`/`tool_use`/`tool_result`/`final`/`text`/`clarify`/`plan`/`plan_progress`。

**约束**：子 agent 的 `EventStream` 应独立创建（不混入父 agent 的流），渲染可走「抑制/聚合摘要」的 transport；或复用同一 transport 但用 `span`/`session` 维度区分。父 agent 拿回的是子 agent 的**文本摘要**，而非其事件流。

### 4.4 Tracer / Span（子 agent span 自动成为父的子 span）

`agent/obs/tracer.py`：`Tracer.span(name, kind, parent)` + `contextvars` 隐式 parent 传递（`contextvars.ContextVar("_current_span")`）。子 agent 的 `model.act`/`tool.exec` 自动成为父 agent 调用链下的子 span（前提是共享同一 `Tracer` 实例，且子 agent `run` 入口的 `reset_current_span()` 需谨慎——见 §6.3）。

### 4.5 Model 抽象（子 agent 模型降级 + 测试替身）

`agent/core/model.py`：`Model` 协议（`act`/`stream`）、`FakeModel(script)`、`RecordingModel(decision|on_act)`、`create_model(settings)`、`OpenAICompatibleModel`。子 agent 可注入更便宜的 `model`（如 `haiku` 对应本项目换个 `llm.model` 配置），测试一律 `FakeModel`/`RecordingModel` 不联网。

### 4.6 Session 层（子 agent 可绕过 Session 直跑 AgentLoop）

`agent/core/session.py`：`Session` 持有 `messages`/模式，`Session.step(task, transport, *, yes, fatal_plan_decline)` 编排澄清回填与计划确认。子 agent **无需走 Session**——直接构造 `AgentLoop(...)` + 独立 `messages=[]` 跑 `run()`，主 agent 把 `run` 结果摘要回填即可。

### 4.7 提示词外置机制（Skill 正文复用）

`agent/core/prompts.py` + `agent/prompts/`：`load_prompt(name).render(**vars)` 的 frontmatter + Jinja2 结构。Skill 的 `SKILL.md` 正文渲染可复用同一 loader（需支持从项目/用户 `.agent/skills/<name>/SKILL.md` 加载，而非仅包内 `prompts/`）。

### 4.8 工具注册（子 agent 工具白名单）

`agent/runtime/registry.py` + `tools/`：`@tool(name, risk, schema)` 装饰器 → `ToolSpec(name, fn, risk, schema).to_openai()`；`ToolRegistry`（构造器 + `register`/`get`/`list`/`run`/`to_openai_tools`）；`default_registry` 全局单例。子 agent 工具白名单 = **新建一个 `ToolRegistry` 子集**（只 `register` 允许的工具），传给 `AgentLoop(registry=subset)`。

### 4.9 审批 / 沙箱（子 agent 权限隔离）

`agent/runtime/approval.py`（`ApprovalGate`）+ `agent/runtime/sandbox.py`（`SandboxExecutor`/`build_executor`/`get_executor`）。子 agent 可继承父配置，或独立构造覆盖（如只读 subagent 用 `plan` 模式 + `read-only` 沙箱 + `gate` 跳过 exec）。

---

## 5. 设计映射与决策建议（Claude → 本项目）

### 5.1 SkillLoader 映射

| Claude | 本项目落地 |
|---|---|
| `.claude/skills/<name>/SKILL.md` | `<project>/.agent/skills/<name>/SKILL.md`（项目级）+ `~/.agent/skills/<name>/SKILL.md`（用户级），对齐既有 `.agent` 隔离约定 |
| frontmatter `name`/`description`/`when_to_use` | `SkillSpec` dataclass；`description`+`when_to_use` 拼成「触发描述」常驻系统提示 |
| `description` 常驻、正文按需加载 | `SkillLoader` 加载时把每个 skill 的「触发描述」注入主循环 system 提示；正文**仅当模型决定调用时才读取并作为单条 user/系统消息注入** |
| `disable-model-invocation` / `user-invocable` / `paths` | `SkillSpec` 字段；`paths` 用 glob 限定自动触发文件范围 |
| `allowed-tools` / `disallowed-tools` | 调用 skill 时，临时并入 `AgentLoop` 的 tool 集合（允许免批工具）；非本次活动后清除 |
| `arguments` / `$ARGUMENTS` / `$name` | `SkillLoader.render_body(args)` 做字符串替换（Jinja2 或简单 `$VAR` 替换） |
| `scripts/` / `${CLAUDE_SKILL_DIR}` | skill 目录即 `${SKILL_DIR}`，脚本用绝对路径执行 |
| `context: fork` + `agent:` | skill 可声明「在 fork subagent 中执行」，委托给 `SubagentSpawner` |

**触发方式决策**：本项目采用 **「描述常驻 + 工具调用」** 双轨（对齐 Claude）：① 把每个 skill 的触发描述作为一条虚拟工具或系统提示片段注入，模型自动匹配；② 模型决定调用时，`SkillLoader` 读正文、替换参数、作为单条消息注入当前 conv（不重新读取）。这与 M1.5 的「控制工具集中化」「提示词外置」一致。

### 5.2 SubagentSpawner 映射

| Claude | 本项目落地 |
|---|---|
| `.claude/agents/<name>.md` | `<project>/.agent/agents/<name>.md`（项目级）+ `~/.agent/agents/<name>.md`（用户级） |
| frontmatter `name`/`description`/`tools`/`model`/`permissionMode`/`maxTurns`/`effort`/`isolation` | `AgentSpec` dataclass；字段尽量对齐 |
| 内置 Explore/Plan/General-purpose | 预置三种 `AgentSpec` 常量（只读/跳过会话文件）；可作为默认 subagent 类型 |
| 独立上下文窗口 | `SubagentSpawner.spawn(spec, task)` 内部 `AgentLoop(model, subset_registry, settings, tracer, sandbox, gate)` + 独立 `messages=[]` + 独立 `ContextManager`；**不共享父 conv** |
| 摘要返回 | 子 agent 的 `AgentResult.text` 即摘要，父 agent 把该文本作为一条 `user` 消息回填（如 `[Subagent <name>] <summary>`） |
| 并行 | `asyncio.gather(*[spawn(...) for ...])` 并行多个 subagent（`AgentLoop` 已是 async） |
| 嵌套深度限制 5 | `SubagentSpawner` 维护 `depth`，构造时 `depth+1`，`depth>=5` 拒绝下发 `spawn_subagent` 工具 |
| fork 模式 | 可选：fork 时把父 `messages` 拷贝给子（共享历史），但工具调用仍在父上下文外；本项目 M5 先实现「独立上下文」主路径，fork 作为可选增强 |
| 转录持久化 | 子 agent 的 `EventStream` 与 `Tracer` 独立保存（M6 会话恢复可复用 sqlite）；M5 先把摘要回填，转录可选落盘 |

### 5.3 上下文隔离映射（核心）

- **独立 `ContextManager` + 独立 `messages`**：父/子互不污染（对齐 Claude「独立上下文窗口」）。
- **事件流隔离**：子 agent 用独立 `EventStream`；父 agent 只接收 `AgentResult.text` 摘要（对齐「摘要返回，详细输出留在子上下文」）。
- **Trace 父子**：子 agent 共享父 `Tracer` 实例 → 子 `model.act`/`tool.exec` 自动成为父调用链下的子 span。**注意**：`AgentLoop.run` 入口有 `Tracer.reset_current_span()`（loop.py:118），这会切断父 span。子 agent 调用需避免 reset（或 reset 后用显式 `parent_override` 指回父 span）。M5 落地时给 `SubagentSpawner` 传入「父 span」并让子 loop 用 `parent_override` 而非 reset。

### 5.4 工具白名单映射

子 agent `tools`/`disallowedTools` → 构造 `ToolRegistry` 子集：从 `default_registry.list()` 过滤（白名单保留 / 黑名单移除），传给 `AgentLoop(registry=subset)`。`AgentLoop._model_tools()` 自动 `registry.list()` + `collect_control_tools()`，无需改 loop。

### 5.5 权限 / 沙箱映射

- `permissionMode: plan` / `read-only` → 子 agent 用 `sandbox=build_executor('local', profile=read-only)` + `gate` 配置为跳过 exec。
- `model` 降级 → `SubagentSpawner` 用另一个 `create_model(...)`（不同 `llm.model`/`base_url`）构造子 `AgentLoop`。
- 子 agent 的审批 HITL：非交互环境下 `ApprovalGate.noninteractive_default` 决定，避免子 agent 卡在人工审批（推荐子 agent 默认 `noninteractive_default="allow"` 或受限沙箱）。

### 5.6 模型降级 + 测试

- 子 agent 测试：**不联网**。直接给 `AgentLoop(FakeModel(script), subset_registry, settings, tracer)` 跑 `run()`，断言返回的 `AgentResult.text` 摘要、`messages` 隔离、工具白名单生效。
- `SkillLoader` 测试：`FakeModel` 驱动主循环决定调用某 skill；断言 skill 正文被正确读取、参数替换、注入 conv。

---

## 6. 踩坑与决策（对齐项目铁律）

1. **loop 实例无状态是最大红利**：子 agent 复用 `AgentLoop` 不需改循环代码，只传独立 `messages`。不要在 `AgentLoop` 里加「当前 agent 身份」状态。
2. **`Tracer.reset_current_span()` 会切断父 span**：子 agent 经 `SubagentSpawner` 调起时，不能在子 `run` 里 reset（否则子 span 跑到根）。方案：子 loop 接收 `parent_override=父span` 并用 `tracer.span(..., parent_override=...)`，跳过 reset。或 `SubagentSpawner` 在 spawn 前后手动 `push/pop` contextvar。
3. **压缩绝不碰 EventStream**：子 agent 的 `ContextManager` 同样遵守 M4.1 铁律（只压 conv 投影，审计真相不可变）。
4. **配对铁律**：子 agent 若用工具，tool_use/tool_result 必须成对（M4 已强调），`AgentLoop._exec_tools` 已保证，子 agent 直接复用。
5. **skill 正文「按需加载」省 token**：绝不让所有 skill 正文常驻 conv（Claude 明确：description 常驻、正文调用时加载）。这是 M5 相对「把所有提示词塞进 system」的关键优化。
6. **摘要返回而非全文**：父 agent 只拿 `AgentResult.text`；子 agent 的搜索结果/文件内容留在子上下文（可被子 agent 自己的 `ContextManager` 压缩）。
7. **嵌套深度**：固定 `depth` 上限（建议 5，对齐 Claude），防失控递归。
8. **子 agent 沙箱默认收紧**：只读/受限 subagent 用 `read-only` 沙箱 + 跳过 exec 的 gate，避免子任务越权改文件。

---

## 7. 落地步骤建议（指向 `milestones/M5-扩展能力/`）

| 步骤 | 内容 | 关键交付 |
|---|---|---|
| **5.1 SkillLoader 基础** | `SkillSpec` + `SkillLoader`（发现/加载 `.agent/skills/*`、解析 frontmatter、触发描述注入、正文按需加载+参数替换） | `agent/skills/loader.py`、`SkillSpec` |
| **5.2 SubagentSpawner** | `AgentSpec` + `SubagentSpawner.spawn()`（独立 loop/conv/ContextManager/Tracer parent、摘要回填、深度限制） | `agent/subagent.py`、`AgentSpec` |
| **5.3 集成与工具白名单** | 主循环接入 skill 触发 + `spawn_subagent` 工具；工具白名单/权限/沙箱映射；fork 可选 | loop 集成 + registry 子集 + gate/sandbox 覆盖 |
| **5.4 CLI 命令** | `/skills` 管理、`/agents` 查看、交互触发 skill、后台 subagent | CLI 扩展 |
| **5.5 测试与验收** | FakeModel 驱动全链路；断言隔离/白名单/摘要/深度/不变量 | `tests/test_skills.py` + `tests/test_subagent.py` |

> 详细三要素（实现方案/验收标准/知识沉淀）见 `milestones/M5-扩展能力/` 各步骤文件。
