# CODEBUDDY.MD — 项目级指令与开发约定

> 本文件是项目级指令。AI 每次开始新会话/新步骤时，**必须先读取本文件**以恢复上下文，并遵循其中的开发模式与约定。
> 配套文档：`通用Agent架构调研与设计报告.md`（架构依据）、`milestones/`（里程碑计划）、`knowledge/INDEX.md`（跨里程碑知识沉淀）、`docs/daemon-api.md`（**daemon WebSocket 接口文档**，前端/后端交互前必查）。

---

## 1. 项目目标

构建一个类似 Claude Code / Codex 的**通用编码 Agent**，覆盖 11 项能力：ReAct 循环、工具调用/注册/沙箱+确认、上下文管理/压缩/长短记忆、Skill 体系/子 Agent 上下文 fork、项目与会话隔离、韧性层（限流/熔断/降级）、意图澄清、配置代码分离、CLI 入口、可观测性（trace/span 父子）、测试驱动。

---

## 2. 开发模式：里程碑 + 步骤（强制）

项目采用**里程碑（Milestone）开发模式**。每个里程碑可拆分为多个**步骤（Step）**。每一步必须包含：

1. **实现方案**：要做什么、改哪些文件、关键接口/算法、依赖的环境或前置步骤。
2. **验收标准**：可验证的完成条件（命令/输出/测试必须通过，最好可自动化）。
3. **知识沉淀**：该步完成后，把"关键决策、踩坑、接口约定、不可遗忘的上下文"写入：
   - 对应里程碑文档 `milestones/<M>.md` 的「知识沉淀」小节；
   - 追加到 `knowledge/INDEX.md`（项目级知识库，供后续步骤检索）。

**铁律**：未通过验收标准不得标记步骤完成；步骤完成后必须沉淀知识，否则后续步骤会丢失上下文。

---

## 3. 知识沉淀机制（防遗忘上下文）

- **来源**：每个完成的 Step。
- **去向**：① `milestones/<M>.md` 的「知识沉淀」小节（里程碑内闭环）；② `knowledge/INDEX.md`（跨里程碑共享，按主题索引）。
- **内容**：只记"后续会用到且易忘"的——接口签名、模块边界、约定、决策理由、坑。不记可重新生成的代码。
- **检索**：开始新步骤前，先读 `knowledge/INDEX.md` 与对应里程碑文档，恢复上下文。

---

## 4. 架构决策摘要（详见调研报告）

- **98/1.6 法则**：AI 只做决策，循环/权限/路由/压缩/持久化全部确定性实现。
- **安全在 OS 层**：沙箱是独立可插拔执行层（Local seccomp / Docker），prompt 仅软约束。
- **上下文稀缺**：静态(系统提示/规则) 与 动态(对话/工具结果) 分离；稳定前缀走 prompt caching；超阈值递归摘要压缩。
- **能力正交**：Tool(原子) / Skill(按需包) / Subagent(隔离上下文) 三层。
- **两条全局主线**：事件流（状态单一事实来源）+ Trace/Span（可观测，OTel 语义，父子 parent_id）。
- **可恢复可观测**：检查点(session_id, sqlite) + trace 贯穿。

---

## 5. 技术栈与工程约定

| 项 | 约定 |
|---|---|
| 语言 | Python 3.12+ |
| CLI | `typer` |
| 异步 | `asyncio`（工具/子 Agent 并行） |
| 模型 | 自抽象 `Model` 接口 + `FakeModel`/`RecordingModel`(测试) / 真实 provider 实现；**provider 无关**：底层走 OpenAI 兼容协议，任何服务（DeepSeek / OpenAI / 本地 vLLM 等）只需改 `<项目根>/.agent/settings.yaml` 的 `llm.api_key` / `llm.base_url` / `llm.model`，**无需改代码**；默认值指向 DeepSeek（`https://api.deepseek.com`，模型 `deepseek-v4-flash`）。模型支持流式：`Model.stream()` 增量产文本、收尾回传 `Decision`。 |
| 配置 | `pydantic-settings` + YAML，分层：内置默认 → 用户级 → 项目级 → CLI 参数 |
| 持久化 | `sqlite`（会话/记忆/事件流），路径 `<project>/.agent/` |
| 可观测 | 自研 `Tracer`(OTel 语义)，可导出 JSON / 接 Langfuse |
| 测试 | `pytest` + `pytest-asyncio`；LLM 一律可 Mock |
| 提示词 | Markdown + frontmatter，与代码分离 |
| 代码质量 | `ruff` 做 lint + format（**CI 强制**，版本锁定 0.16.0）；禁止 lambda 赋变量（E731，一律用 `def`）；提交前完整门禁见 §7.2 |

**目录约定**（详见报告 §5，以当前仓库为准）：
- `agent/core`(循环/意图/模型)、`agent/runtime`(注册/审批/沙箱)、`agent/context`(管理/压缩/记忆)
- `agent/daemon`(agentrunner 守护进程 + WebSocket 协议，**双端契约唯一事实来源见 `agent/daemon/protocol.py`**)、`agent/tui`(Textual 全屏 TUI)
- `agent/tools`、`agent/skills`、`agent/prompts`(Markdown 提示词)、`agent/testing`(FakeModel/RecordingModel/tape)、`agent/resilience`(限流/熔断/降级)、`agent/obs`(Trace/Span)、`agent/config`、`agent/subagent.py`
- `desktop/`(Electron + React + TypeScript 前端，经 WebSocket 连 daemon)、`scripts/`、`tests/`、`milestones/`、`knowledge/`

**隔离约定**：项目数据在 `<project>/.agent/`（settings.yaml、AGENTS.md、skills/、sessions/）；用户级在 `~/.agent/`；优先级 项目 > 用户。

---

## 6. 关键文件索引

- 架构依据：`通用Agent架构调研与设计报告.md`
- 里程碑总览：`milestones/README.md`
- 里程碑模板：`milestones/template.md`
- 里程碑目录：`milestones/<Mx-名称>/`（每步一个 `.md` 文件，均含三要素）
- 知识库：`knowledge/INDEX.md`
- **Daemon 接口文档**：`docs/daemon-api.md`（前端/后端经 WebSocket 通信的**权威接口参考**；后端改 daemon 接口须同步更新本文件 + `agent/daemon/protocol.py` + `desktop/src/protocol/types.ts`；前端交互前必须先查）
- 环境变量：`AGENT_PROJECT_ROOT` / `AGENT_USER_CONFIG_DIR`（覆盖配置目录；`AGENT_PROJECT_ROOT` 也用于从其他目录启动）。密钥 `llm.api_key` 写在 `<项目根>/.agent/settings.yaml`（已被 `.gitignore` 忽略），**不读 `.env`**

---

## 7. 需求分类与开发工作流（强制）

每次接到需求，**第一步必须先判定影响范围**，再动手。判定维度（以当前仓库结构为准）：

| 维度 | 含义 | 对应代码 |
|---|---|---|
| 前端（Frontend） | 仅涉及 UI / 交互 / 渲染 | `desktop/`（Electron + React + TypeScript） |
| 后端（Backend） | 仅涉及 Agent 内核 / 工具 / 模型 / 持久化 / 守护进程 | `agent/`（Python，含 `daemon/`、`tui/`、`tools/`、`skills/` 等） |
| 双端（Both） | 既改前端又改后端，或涉及两端通信契约 | 上述都涉及（典型：前端经 WebSocket 连 `agent/daemon`） |

### 7.1 判定与对齐规则

1. **先判定，后动手**：开干前用一句话明确"本次是前端 / 后端 / 双端"，并在方案里写清；双端任务需在方案里点明契约影响点。
2. **双端必须对齐协议**：本仓库的「双端」即 `desktop/`(前端 Electron) 与 `agent/daemon`(后端常驻 daemon)，二者经 **WebSocket** 通信。**契约的消息类型唯一事实来源是 `agent/daemon/protocol.py`**（`PROTOCOL_VERSION`、`MsgType` 枚举、`parse_message`/`make_message`）；**可读接口文档（payload/行为/流程）见 `docs/daemon-api.md`**，供前端交互需求查阅、后端改接口时同步更新。任何双端改动都必须先在此冻结/演进契约，前端 WS 客户端须严格与之对齐（消息类型、字段名、类型、错误码），禁止前端另写一套导致对不上；`PROTOCOL_VERSION` 变化需同步知会对方侧并评估兼容。三处须保持同步：**`protocol.py`（MsgType 唯一事实来源）+ `docs/daemon-api.md`（接口文档）+ `desktop/src/protocol/types.ts`（前端强类型投影）**，契约测试 `scripts/check-msgtype.mjs` 保证三者 MsgType 一致。
3. **不确定就提问**：出现以下任一情况，**必须先向用户提问澄清，禁止凭猜测开工**：
   - 无法判定属于前端还是后端，或边界模糊；
   - 双端契约（消息类型、字段名、类型、行为）存在歧义或未定义；
   - 需求描述缺失关键场景（边界、异常、权限、兼容性）；
   - 改动可能影响现有里程碑 / 接口且不能确定是否破坏。
   - 提问要具体、给选项（用 `ask_followup_question` 或明确的多选式追问），得到确认后再继续。

### 7.2 Python（后端）改动提交前质量门禁（强制）

任何涉及 `agent/`、`scripts/` 等 Python 代码的改动，**提交（commit/push）前必须本地依次跑通以下三项且全绿**（版本已锁定：ruff 0.16.0、basedpyright 1.39.9，勿随意升级以免 CI 漂移）：

```bash
# 1) 代码质量：lint + 格式（CI 强制，含 E731 禁止 lambda 赋变量等规则）
ruff check .
ruff format --check .
# 2) 类型检查：basedpyright 仅对 agent/ + scripts/ 做检查（tests 不纳入），CI 门禁，远端会阻断 PR
basedpyright
# 3) 测试：默认跳过 slow 标记用例；LLM 一律用 FakeModel/RecordingModel 等 Mock，可离线跑
pytest -q
#   （nightly/手动慢测：pytest -m slow）
```

- 三项任一不通过，**不得标记完成、不得提交**。
- 注意：按项目约定，git commit / git push 本身也需用户明确同意——门禁通过后停下来等用户拍板。

### 7.3 前端（TypeScript/Electron）改动门禁（建议强制）

`desktop/` 改动提交前建议跑通其自带门禁（`cd desktop`）：

```bash
npm run typecheck   # tsc --noEmit，类型检查
npm run test        # vitest run，单元测试
npm run build       # electron-vite build，确保可构建（含导入/打包错误）
```

- 前端无 basedpyright；类型安全靠 `tsc`；契约一致性靠与 `agent/daemon/protocol.py` 对齐（见 7.1）。

---

## 8. 常用命令

### 8.1 后端（Python）

```bash
# 安装(开发)：含 ruff/basedpyright/pytest 等，版本见 pyproject 锁定
pip install -e ".[dev]"
# 跑测试（默认跳过 slow；pytest 配置见 pyproject）
pytest -q
pytest --cov=agent          # 带覆盖
pytest -m slow              # 仅跑慢/端到端用例

# CLI 入口（typer）：run / chat / resume / fork / daemon / client / health / init
python -m agent.cli run "把 TODO 注释清理掉"   # 一次性执行
python -m agent.cli chat                      # 默认 Textual 全屏 TUI；非 TTY 自动退回旧渲染
python -m agent.cli chat --legacy             # 强制旧版非全屏渲染
python -m agent.cli resume <session_id>       # 恢复会话
python -m agent.cli fork <session_id>         # 从会话派生分支
python -m agent.cli daemon                    # 启动 agentrunner 守护进程（前端经 WS 连接）
python -m agent.cli client -p <port>          # 连 daemon 的 CLI 客户端
python -m agent.cli health                    # 组件健康检查
python -m agent.cli init                      # 生成 .agent/ 配置骨架

# 质量门禁（CI 强制，提交前必跑，详见 §7.2）
ruff check .                # lint
ruff format .              # 自动格式化
ruff format --check .      # 仅检查格式
basedpyright                # 类型检查（仅 agent/ + scripts/）
```

### 8.2 前端（TypeScript/Electron，在 desktop/ 下）

```bash
npm install
npm run dev        # electron-vite dev
npm run typecheck  # tsc --noEmit
npm run test       # vitest run
npm run build      # electron-vite build
```
