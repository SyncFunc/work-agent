# 里程碑 M5 扩展能力

> 目标：交付 `SkillLoader` + `SubagentSpawner`，让 Agent **可组合、可伸缩**。
> 设计依据：`knowledge/claude-code-subagents-skills.md`（Subagents/Skills 机制调研 + 本项目对接点）。

## 目标

把「能力正交」三层（Tool 原子 / Skill 按需包 / Subagent 隔离上下文）补齐后两层：

- **SkillLoader**：把可复用提示词 + 脚本 + 参考文档打包成 Skill，触发描述常驻上下文、正文按需加载（省 token），支持参数替换与多文件包。
- **SubagentSpawner**：把子任务委派给独立上下文窗口的分身，主上下文只拿回摘要，突破上下文瓶颈；支持并行、嵌套（深度限制）、模型降级、工具白名单、独立沙箱/权限。

## 前置依赖

| 里程碑 | 状态 | 本里程碑依赖的内容 |
|---|---|---|
| M1 骨架 | 进行中 | `AgentLoop`(无状态)/`AgentResult`/`Model`(`FakeModel`/`RecordingModel`)/`Session`/`AgentTransport`/`EventStream`/`load_prompt` |
| M2 安全与确认 | 进行中 | `ApprovalGate`/`SandboxExecutor`/`build_executor`/`ToolRisk`（子 agent 权限/沙箱隔离） |
| M3 可观测与韧性层 | 进行中 | `Tracer`/`Span`/`contextvars` 隐式 parent（子 agent trace 成父子 span） |
| M4 上下文与记忆 | 进行中 | `ContextManager`/`compact()`（子 agent 独立上下文 + 压缩，绝不碰 EventStream） |

> M5 全部复用既有接口，**不改动 M1–M4 的核心实现**；仅 M5.3 在 `AgentLoop` 接入 skill 触发工具与 `spawn_subagent` 工具（分支处理，复用 `_exec_tools` 的 tool_call_id 配对）。

## 步骤索引

| 步骤 | 文件 | 目标 |
|---|---|---|
| M5.1 | [5.1-SkillLoader基础.md](./5.1-SkillLoader基础.md) | `SkillSpec` + `SkillLoader`：发现/解析 frontmatter、触发描述注入、正文按需加载+参数替换、多文件包 |
| M5.2 | [5.2-SubagentSpawner.md](./5.2-SubagentSpawner.md) | `AgentSpec` + `SubagentSpawner.spawn()`：独立 loop/conv/ContextManager/Tracer parent、摘要回填、深度限制、内置类型 |
| M5.3 | [5.3-集成与工具白名单.md](./5.3-集成与工具白名单.md) | 主循环接入 skill 触发 + `spawn_subagent` 工具；工具白名单/权限/沙箱映射；fork 可选 |
| M5.4 | [5.4-CLI命令.md](./5.4-CLI命令.md) | `/skills` 管理、`/agents` 查看、交互触发 skill、后台 subagent |
| M5.5 | [5.5-测试与验收.md](./5.5-测试与验收.md) | FakeModel 驱动全链路；断言隔离/白名单/摘要/深度/不变量 |

## 关键设计决策（跨步骤，详见调研文档 §5–§6）

1. **Skill 与 Subagent 的存放路径对齐 `.agent` 约定**：项目级 `<project>/.agent/skills/<name>/SKILL.md` 与 `<project>/.agent/agents/<name>.md`；用户级 `~/.agent/skills/` 与 `~/.agent/agents/`；**项目级 > 用户级**（优先级与既有隔离约定一致）。
2. **Skill 触发采用「描述常驻 + 工具调用」双轨**：每个 skill 的 `description`(+`when_to_use`) 拼成触发描述注入系统提示（常驻、低成本）；模型决定调用时 `SkillLoader` 才读正文、替换参数、作为单条消息注入 conv（绝不让所有正文常驻）。
3. **Subagent 复用无状态 `AgentLoop`**：只传独立 `messages=[]` 即隔离上下文；可注入不同 `model`/`registry`/`sandbox`/`gate`。
4. **摘要返回**：父 agent 只拿 `AgentResult.text`，子 agent 的搜索结果/文件内容留在子上下文（可由子 `ContextManager` 压缩）。
5. **Trace 父子**：子 agent 共享父 `Tracer`，但 `AgentLoop.run` 入口的 `Tracer.reset_current_span()` 会切断父 span——子 spawn 必须用 `parent_override` 指回父 span（见 5.2）。

## 里程碑级知识沉淀

> 本里程碑全部步骤完成后，汇总跨步骤结论。
