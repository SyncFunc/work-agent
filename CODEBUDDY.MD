# CODEBUDDY.MD — 项目级指令与开发约定

> 本文件是项目级指令。AI 每次开始新会话/新步骤时，**必须先读取本文件**以恢复上下文，并遵循其中的开发模式与约定。
> 配套文档：`通用Agent架构调研与设计报告.md`（架构依据）、`milestones/`（里程碑计划）、`knowledge/INDEX.md`（跨里程碑知识沉淀）。

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

**目录约定**（详见报告 §5）：`agent/core`(循环/意图/模型)、`agent/runtime`(注册/审批/沙箱)、`agent/context`(管理/压缩/记忆)、`agent/skills`、`agent/subagent.py`、`agent/resilience`、`agent/obs`、`agent/config`、`tools/`、`skills/`、`tests/`、`milestones/`、`knowledge/`。

**隔离约定**：项目数据在 `<project>/.agent/`（settings.yaml、AGENTS.md、skills/、sessions/）；用户级在 `~/.agent/`；优先级 项目 > 用户。

---

## 6. 关键文件索引

- 架构依据：`通用Agent架构调研与设计报告.md`
- 里程碑总览：`milestones/README.md`
- 里程碑模板：`milestones/template.md`
- 里程碑目录：`milestones/<Mx-名称>/`（每步一个 `.md` 文件，均含三要素）
- 知识库：`knowledge/INDEX.md`
- 环境变量：`AGENT_PROJECT_ROOT` / `AGENT_USER_CONFIG_DIR`（覆盖配置目录；`AGENT_PROJECT_ROOT` 也用于从其他目录启动）。密钥 `llm.api_key` 写在 `<项目根>/.agent/settings.yaml`（已被 `.gitignore` 忽略），**不读 `.env`**

---

## 7. 常用命令

```bash
# 安装(开发)
pip install -e ".[dev]"
# 跑测试
pytest -q
# 跑单测带覆盖
pytest --cov=agent
# 启动交互
python -m agent.cli chat
# 一次性执行
python -m agent.cli run "把 TODO 注释清理掉"
# 恢复会话
python -m agent.cli resume <session_id>
```
