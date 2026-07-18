# 通用编码 Agent 架构调研与设计报告

> 目标：构建类似 Claude Code / Codex 的通用 Agent，覆盖 ReAct 循环、工具调用/注册/沙箱+确认、上下文管理/压缩/长短记忆、Skill 体系/子 Agent 上下文 fork、项目与会话隔离、韧性层、意图澄清、配置代码分离、CLI 入口、可观测性（trace/span 父子）、测试驱动 11 项能力。
>
> 调研基准（2026-07）：Claude Code、OpenAI Codex CLI、LangGraph、OpenHands、Aider、Cline、AutoGen/CrewAI。
> 建议实现语言：**Python 3.12+**（生态成熟、测试工具完善、与 LLM SDK 集成度高）。

---

## 0. 执行摘要

核心结论先讲三点，它们决定了整个架构的"形状"：

1. **98 / 1.6 法则（Dive into Claude Code, 2026）**：Claude Code 代码库中仅约 1.6% 是 AI 决策逻辑，其余 98.4% 都是**确定性基础设施**——权限门、上下文管理、工具路由、沙箱、事件流。这提示我们：**Agent 的工程价值主要在"外壳"而非"模型"**。模型可替换，基础设施决定体验与下限。
2. **安全在 OS 层，不在 Prompt 层（Codex 洞察）**：Codex 用 macOS Seatbelt / Linux Landlock+seccomp 在内核级做沙箱，而不是靠"请模型不要做坏事"。这决定了沙箱必须是一个独立的、可插拔的执行层。
3. **上下文是稀缺资源（LangChain/Claude 共识）**：长程任务上下文成本 O(N²) 增长，且存在"lost-in-the-middle"注意力衰减。必须做静态/动态分离、prompt caching、主动压缩、文件系统外置。

基于此，本报告收敛到一套**分层、正交、可插拔**的架构：AI 只负责"决策"（选工具/产出文本），其余全部用确定性组件实现，并以"事件流 + trace"作为贯穿全局的单一事实来源。

---

## 1. 调研对象与核心机制拆解

### 1.1 Claude Code

| 维度 | 设计 |
|---|---|
| **ReAct 循环** | 系统提示 + 工具 Schema + 用户消息 → LLM → 解析 `tool_use` → 执行 → 把 `tool_result` 回灌上下文 → 再次调用，直到产出最终文本或 `task_complete`。循环由主 Agent 驱动。 |
| **权限模型** | 3 种模式：`default`（每步确认）、`acceptEdits`（自动接受编辑但确认命令）、`bypassPermissions`（全放开）。`allow`/`deny` 规则 + `canUseTool` 回调实现精细控制。模型可用 `AskUserQuestion` 工具主动澄清。 |
| **上下文管理** | `CLAUDE.md`（项目/用户级常驻系统提示）、`/compact` 递归摘要压缩、自动压缩阈值触发、Prompt Caching 缓存稳定前缀。 |
| **记忆** | 四级：系统提示 / `CLAUDE.md` / `MEMORY.md`（自动记忆，遇重要事实写入）/ 会话历史。`@import` 支持分模块加载。 |
| **Skill 体系** | Skill = 按需加载的能力包（frontmatter + 指令 + 脚本），平时不占上下文，被 `Skill` 工具调用时注入。解决"常驻规则臃肿"问题。 |
| **Subagent** | 每个 Subagent 在**独立 context window** 运行，有自定义 system prompt、工具白名单、独立权限。由主 Agent 用 `Task` 工具 fork。解决"辅助任务污染主上下文"。`fork`/`agent:type` 参数让复杂 Skill 在子 Agent 中跑，不占主上下文。 |
| **项目隔离** | 项目级 `.claude/settings.json` + `CLAUDE.md`；用户级 `~/.claude/`。优先级：项目 > 用户。 |
| **可观测** | 事件流（Event Stream）、`/cost`、`/status`。 |

### 1.2 OpenAI Codex CLI

| 维度 | 设计 |
|---|---|
| **架构** | Rust core（执行/沙箱/审批）+ Node/TS 包装层。性能与隔离更好。 |
| **审批策略** | `AskForApproval` 枚举：`untrusted`（全确认）、`on-request`、`on-failure`（失败才确认）、`never`（全自动）。 |
| **沙箱** | **OS 级**纵深防御：macOS Seatbelt、Linux Landlock+seccomp。三种 profile：`read-only`、`workspace-write`、`danger-full`。默认拒绝网络，工作区可写。容器/CI 内运行时可声明 `ExternalSandbox` 让外层负责隔离。 |
| **Exec 规则引擎** | `ExecPolicy` 用命令前缀匹配决定允许/拒绝/需审批，可声明式配置。 |
| **Session 模型** | 每次任务是一个 `rollout`，状态可持久化与恢复。 |
| **记忆** | `AGENTS.md` 持久项目记忆。 |
| **可观测** | 逐 rollout 的 trace、token 成本、步骤回放。 |

### 1.3 主流框架对照

| 框架 | 核心范式 | 对我们有借鉴的部分 |
|---|---|---|
| **LangGraph** | 状态图（`StateGraph`，节点+边），检查点持久化（`thread_id`），`interrupt()` 实现人机协同，原生 retry/fallback。 | **检查点=可恢复状态**、HITL 中断原语、错误边（retry/fallback/compensation）。 |
| **OpenHands** | 控制器 + Agent + Runtime（沙箱）三层，以**事件流（Event Stream）为单一事实来源**，Docker/e2b 作 runtime。 | **事件流**作为可重放、可观测、可持久化的核心抽象。 |
| **Aider** | 极简，git 驱动，用 `repo map`（压缩的代码库表示）最小化上下文。 | 上下文压缩的"代码库摘要"思路；git 作为天然的检查点/回滚。 |
| **Cline** | VS Code 内流式工具确认，逐工具权限授予。 | 流式确认 UX、权限授予的渐进式持久化。 |
| **AutoGen/CrewAI** | 多 Agent 角色扮演、对话编排。 | 角色化子 Agent 编排、群聊收敛。 |

**关键共识**：① 用事件流/检查点统一状态；② 沙箱独立；③ 上下文压缩是刚需；④ HITL 与重试是生产级标志。

---

## 2. 设计哲学与原则

基于调研，提出 9 条设计原则（按优先级）：

1. **P1 确定性基础设施优先（98/1.6 法则）**：AI 只做决策，循环、权限、路由、压缩、持久化全部确定性实现，且可独立测试。
2. **P2 安全在边界不在提示**：沙箱是内核/容器级的可插拔执行层，prompt 只是软约束。
3. **P3 最小权限 + 纵深防御**：默认拒绝网络/危险命令，权限分层（模式 → 规则 → 单步确认），永不信任 Agent 输出。
4. **P4 上下文是稀缺资源**：静态（系统提示/规则）与动态（对话/工具结果）分离；稳定前缀走 prompt caching；超阈值主动压缩；可外置到文件系统。
5. **P5 能力正交与可组合**：Tool（原子能力）/ Skill（按需能力包）/ Subagent（隔离上下文）三层职责清晰、可独立注册与测试。
6. **P6 人机协同默认开启**：以"确认疲劳"与"安全事故"为两端，提供多级模式，把澄清前置（意图澄清）。
7. **P7 可恢复 & 可观测**：任何时刻状态可检查点、可暂停、可恢复；每次调用有 trace/span，父子可追溯。
8. **P8 配置与代码分离**：能力、策略、权限、提示词用声明式配置（YAML/Markdown），代码只读配置。
8+. **P9 韧性内建**：限流/熔断/降级是执行层的一等公民，不是事后补丁。

---

## 3. 收敛方案：分层架构

### 3.1 总体架构（自上而下）

```
┌──────────────────────────────────────────────────────────────┐
│  CLI 入口层 (typer/click)  : run / chat / config / eval        │
├──────────────────────────────────────────────────────────────┤
│  Agent 编排层 (ReAct Loop + Scheduler)                         │
│   - AgentLoop: 决策→工具→观察 循环驱动                          │
│   - Planner/Intent: 意图澄清、任务分解                          │
├──────────────────────────────────────────────────────────────┤
│  工具运行时层 (Tool Runtime)                                   │
│   - ToolRegistry: 注册/发现/校验                               │
│   - ApprovalGate: 模式/规则/单步确认（HITL）                   │
│   - SandboxExecutor: OS 级隔离执行（seccomp/Landlock/docker）  │
├──────────────────────────────────────────────────────────────┤
│  上下文 & 记忆层 (Context/Memory)                              │
│   - ContextManager: 静态/动态分离 + prompt cache               │
│   - Compactor: 阈值触发递归摘要                                 │
│   - MemoryStore: 短期(会话)/长期(项目 MEMORY.md)               │
├──────────────────────────────────────────────────────────────┤
│  Skill / Subagent 层                                           │
│   - SkillLoader: 按需加载能力包                                │
│   - SubagentSpawner: fork 独立 context，工具/权限白名单         │
├──────────────────────────────────────────────────────────────┤
│  韧性层 (Resilience)  : 限流 / 熔断 / 降级 / 重试/退避         │
├──────────────────────────────────────────────────────────────┤
│  可观测层 (Observability) : Trace/Span(父子) + 事件流 + 指标    │
├──────────────────────────────────────────────────────────────┤
│  配置 & 存储层 (Config/Storage) : 项目隔离 + 会话隔离 + 声明式  │
└──────────────────────────────────────────────────────────────┘
```

全局贯穿两条主线：**事件流（Event Stream）**作为状态单一事实来源（借鉴 OpenHands），**Trace/Span**作为可观测主线（借鉴 OpenTelemetry）。

---

### 3.2 11 项能力逐一设计

#### ① ReAct 循环
- 实现为 `AgentLoop`：`while not done: decision = llm(messages); if tool_call: result = runtime.execute(tool_call); messages.append(result); else: emit_final(); break`。
- 每轮用 `Span` 包裹（见⑩），工具执行失败触发重试/降级（见⑥）。
- 支持 `max_iterations` 防止失控循环；循环状态写入检查点（见⑦/项目隔离）。

```python
# core/loop.py
async def run(self, task: str) -> AgentResult:
    async with self.tracer.span("agent.run", kind="agent") as span:
        self.context.append(user(task))
        for _ in range(self.max_iterations):
            decision = await self.model.act(self.context.compile())
            if decision.tool_calls:
                for tc in decision.tool_calls:
                    result = await self.runtime.execute(tc)   # → ApprovalGate → Sandbox
                    self.context.append(tool_result(tc, result))
            else:
                return AgentResult(content=decision.text, span=span.id)
        raise LoopMaxIteration()
```

#### ② 工具调用 / 注册 / 沙箱 + 用户确认
- **ToolRegistry**：装饰器 `@tool(name, schema, risk)` 注册；工具自描述 JSON Schema，自动注入系统提示。
- **SandboxExecutor**：抽象 `Executor` 接口，内置实现：`LocalExecutor`（seccomp/Landlock，默认网络拒绝）、`DockerExecutor`（容器隔离）、`ExternalExecutor`（外层已沙箱时直通）。
- **ApprovalGate**：三级——模式（`default`/`acceptEdits`/`auto`）、规则（`allow`/`deny` 前缀匹配，借鉴 Codex ExecPolicy）、单步 HITL 回调（借鉴 `canUseTool`）。
- **风险分级**：`read` / `edit` / `exec` / `network`；高风险默认确认。

```python
# tools/registry.py
TOOLS: dict[str, ToolSpec] = {}
def tool(name, risk="read", allow=None):
    def deco(fn): TOOLS[name] = ToolSpec(name, fn, risk, allow); return fn
    return deco

# runtime/approval.py
def decide(action, policy) -> Decision:
    if policy.mode == "auto" and action.risk == "read": return ALLOW
    if matches(policy.deny, action): return DENY
    if matches(policy.allow, action): return ALLOW
    return ASK_USER   # HITL
```

#### ③ 上下文管理 / 压缩 / 长短期记忆
- **ContextManager**：把上下文切成区块——系统提示(静态, cacheable) / 规则与 Skill(半静态) / 对话与工具结果(动态) / 压缩摘要(动态)。稳定前缀打 `cache_control` 走 prompt caching。
- **Compactor**：当 token 数 > `compact_threshold`（如 0.7×窗口），触发递归摘要：把旧工具结果/对话折叠成结构化摘要（保留决策、路径、错误），保留最近 K 轮原文。
- **MemoryStore**：短期=会话内存（环形缓冲）；长期=项目 `MEMORY.md`（借鉴 Claude，遇重要事实写入，下次加载）；跨项目=用户级 `USER.md`。

```python
# context/manager.py
class ContextManager:
    def compile(self) -> list[Message]:
        return [self.system(cached=True), *self.rules, *self.summary, *self.history]
    def maybe_compact(self):
        if self.tokens() > self.threshold:
            self.summary.append(self.llm.summarize(self.history))
            self.history = self.history[-self.keep_recent:]
```

#### ④ Skill 体系 + 子 Agent 上下文 fork
- **SkillLoader**：Skill = `name.md`（frontmatter: `name/description/allowed-tools`）+ 可选脚本。平时不进上下文，被调用时（模型选 `Skill` 工具或主 Agent 路由）注入对应区块。
- **SubagentSpawner**：`fork(task, tools, permission)` 创建**全新 context window** 的子 Agent（借鉴 Claude Subagent），执行完只回传结论/产物，不污染主上下文。支持嵌套与并行（`asyncio.gather`）。
- 复杂 Skill 可在子 Agent 内执行（借鉴 `agent:type` 参数），实现"嵌套 Skill 不占主上下文"。

```python
# agent/subagent.py
async def spawn(self, spec: SubagentSpec, task: str) -> SubResult:
    child = AgentLoop(tools=spec.tools, policy=spec.policy, ctx=ContextManager.empty())
    return await child.run(task)   # 独立上下文，仅回传结果
```

#### ⑤ 项目隔离 & 会话隔离
- **项目隔离**：`<project>/.agent/` 存 `settings.yaml`、`AGENTS.md`(项目记忆)、`skills/`、`tools/`。优先级：项目 > 用户(`~/.agent/`)。
- **会话隔离**：每次 `run` 生成 `session_id`，状态存 `<project>/.agent/sessions/<id>/`（事件流 + 检查点 + trace）。支持 `resume <id>` 恢复。
- 配置与数据严格分离：代码在包内，用户产物在 `.agent/`。

#### ⑥ 韧性层：限流 / 熔断 / 降级
- 借鉴 Resilience4j / LangGraph retry 边，独立 `Resilience` 模块：
  - **限流 RateLimit**：令牌桶限制 LLM/工具 QPS，超阈排队/拒绝。
  - **熔断 CircuitBreaker**：LLM 或外部工具连续失败后熔断，快速失败。
  - **降级 Degrade**：LLM 不可用时降级到规则/缓存答案或提示用户；工具失败时返回安全占位而非崩溃。
  - **重试+退避**：指数退避 + 抖动，区分可重试/不可重试错误。

```python
# resilience/guard.py
@circuit_breaker(failures=5, cooldown=30)
@retry(times=3, backoff=expo)
@rate_limit(rps=2)
async def call_model(req): ...
```

#### ⑦ 意图澄清
- **IntentClarifier**：在 ReAct 前插入"澄清门"——若任务缺失关键约束（目标模糊/作用域不明/有歧义），用 `AskUserQuestion` 结构化提问（借鉴 Claude），而非直接行动。
- 与权限确认复用同一 HITL 通道，但语义不同（澄清=问"做什么"，确认=问"能不能做"）。
- 提供 `--yes`/`--ask` 开关控制是否自动澄清。

#### ⑧ 配置与代码分离
- 所有可变项（模型选择、权限规则、沙箱 profile、token 阈值、retry 参数、Skill/Tool 启用）走声明式 `settings.yaml` + Markdown 提示词，**代码零硬编码策略**。
- 配置层级：内置默认 → 用户级 → 项目级 → CLI 参数（后者覆盖前者），借鉴 Claude/Codex 的分层配置。

#### ⑨ CLI 入口
- `typer` 实现子命令：
  - `agent run "<task>"`：一次性执行并返回结果（非交互/CI 友好）。
  - `agent chat`：交互式 REPL（流式输出、逐工具确认）。
  - `agent config` / `agent skills list` / `agent eval` / `agent resume <id>`。
- 退出码规范化（0 成功 / 非 0 失败 / 2 需用户确认中断）。

#### ⑩ 可观测性：Trace / Span（父子）
- 采用 **OpenTelemetry 语义**自建轻量实现（或接 OTel SDK）：`Tracer` 产生 `Trace`（一次 run），内含 `Span` 树（agent.run → model.act → tool.execute → sandbox.run）。
- `Span` 记录：开始/结束、输入摘要、token、耗时、状态、父子 `parent_id`。支持导出 JSON / OTLP / 接 Langfuse。
- 与**事件流**合并：每个 Span 对应若干事件（tool_use/tool_result/approval），既可用于回放也用于调试。

```python
# obs/tracer.py
class Tracer:
    def span(self, name, kind, parent=None): ...
# 父子：child = tracer.span("tool.exec", parent=agent_span)
```

#### ⑪ 测试驱动
- **分层测试金字塔**：
  - 单元测试：Tool 实现、Compactor、ApprovalGate、RateLimit/CB（确定性，mock LLM）。
  - 集成测试：用 `FakeModel`（固定响应）跑完整 `AgentLoop`，断言事件流与最终状态。
  - 行为/Eval 测试：pytest + 数据集，用 LLM-as-judge 或确定性强校验（文件是否被改、命令是否执行）评估任务成功率（借鉴 Anthropic "Demystifying evals"）。
- **LLM 可 Mock**：定义 `Model` 抽象接口，`FakeModel`/`RecordingModel` 用于测试；生产接真实 provider。
- CI：每次提交跑单测 + 少量 eval，防止回归。

---

## 4. 建议技术选型（Python）

| 关注点 | 选型 | 理由 |
|---|---|---|
| CLI | `typer` | 类型安全、子命令清晰 |
| 异步 | `asyncio` | 工具/子 Agent 并行 |
| LLM | `openai` / `anthropic` SDK + 自抽象 `Model` | 可替换 provider |
| 配置 | `pydantic-settings` + YAML | 分层校验 |
| 沙箱 | `subprocess` + `seccomp`/`Landlock`(Linux) / Docker | OS 级隔离 |
| 持久化 | `sqlite`（会话/记忆/事件流）| 零依赖、可查询 |
| 可观测 | 自研 `Tracer`(OTel 语义) / 可选 `langfuse` | 轻量起步 |
| 测试 | `pytest` + `pytest-asyncio` | 生态标准 |
| 提示词 | Markdown + frontmatter | 配置代码分离 |

---

## 5. 推荐目录结构

```
work-agent/
├── pyproject.toml
├── agent/
│   ├── cli.py                # ⑨ CLI 入口
│   ├── core/
│   │   ├── loop.py           # ① ReAct 循环
│   │   ├── intent.py         # ⑦ 意图澄清
│   │   └── model.py          # Model 抽象 + 真实/Fake 实现
│   ├── runtime/
│   │   ├── registry.py       # ② 工具注册
│   │   ├── approval.py       # ② 审批门 / HITL
│   │   └── sandbox.py        # ② OS 级执行器
│   ├── context/
│   │   ├── manager.py        # ③ 上下文(静态/动态分离+cache)
│   │   ├── compact.py        # ③ 压缩
│   │   └── memory.py         # ③ 长短记忆
│   ├── skills/               # ④ Skill 加载
│   ├── subagent.py           # ④ 子 Agent fork
│   ├── resilience/           # ⑥ 限流/熔断/降级
│   ├── obs/tracer.py         # ⑩ Trace/Span
│   ├── config/               # ⑧ 分层配置 + 存储
│   └── storage.py            # ⑤ 项目/会话隔离(sqlite)
├── tools/                    # 内置工具(bash/read/write/grep/...)
├── skills/                   # 内置 skill 包
├── tests/                    # ⑪ 单测/集成/eval
└── docs/
```

---

## 6. 分阶段落地路线

- **M1 骨架**：CLI + `Model` 抽象 + `AgentLoop`（接 FakeModel 跑通空转）+ `ToolRegistry` + 基础 `bash`/`read`/`write` 工具。可端到端演示但无安全/压缩。
- **M2 安全与确认**：`ApprovalGate` + `SandboxExecutor` + 分层权限配置（⑧）。达到"能安全跑命令"。
- **M3 上下文与记忆**：`ContextManager` + `Compactor` + `MemoryStore` + prompt caching。达到"长任务不爆窗口"。
- **M4 扩展能力**：`SkillLoader` + `SubagentSpawner` + 意图澄清（⑦）。达到"可组合、可伸缩"。
- **M5 生产化**：韧性层（⑥）+ 可观测（⑩）+ 会话恢复（⑤）+ 测试金字塔（⑪）+ CI。

---

## 7. 关键权衡与风险

| 权衡 | 选择 | 理由 |
|---|---|---|
| 自研 vs 基于 LangGraph | **自研轻量循环 + 借鉴其检查点/HITL 原语** | 编码 Agent 循环简单，自研可控；避免被重框架绑定，但复用其 retry/interrupt 思想。 |
| 沙箱深度 | **先做 Local(seccomp) + Docker 可选** | 内核级更稳但跨平台成本高；Docker 在 CI/高危场景兜底。 |
| 上下文压缩 | **递归摘要 + 保留近 K 轮** | 比丢弃更保真；需评估压缩失败风险（保留原始归档备查）。 |
| 多模型支持 | **Model 抽象 + provider 插件** | 避免绑定单一厂商，符合"配置代码分离"。 |
| 子 Agent 通信 | **仅回传结论/产物** | 防止上下文污染，但可能丢中间推理；提供"verbose"模式回传全文。 |

---

## 8. 结论

本方案把 11 项能力收敛为**一条 ReAct 主线 + 八条确定性生活基础设施**，并以**事件流**和**Trace/Span**两条主线贯穿。它直接吸收：Claude Code 的 98/1.6 法则、权限三级与 Subagent 隔离；Codex 的 OS 级沙箱与 ExecPolicy；LangGraph 的检查点/HITL/retry；OpenHands 的事件流单一事实来源。最终产出一个**可测试、可恢复、可观测、安全隔离**的通用编码 Agent，且模型可插拔、策略全声明式。

下一步建议从 **M1 骨架**开始，用 FakeModel 先把循环与工具注册打通，再逐层叠加安全、压缩、扩展与生产化能力。
