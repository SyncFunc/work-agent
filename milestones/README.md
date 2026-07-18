# 里程碑总览

> 开发模式详见根目录 `CODEBUDDY.MD`。**每个里程碑一个文件夹 `milestones/<Mx-名称>/`，每步一个 `.md` 文件**，均含「实现方案 / 验收标准 / 知识沉淀」三要素。
> 当前进度：M1 进行中，M2–M5 待启动（启动前用 `milestones/template.md` 建文件夹与步骤文件）。

| 里程碑 | 目标 | 关键交付 | 状态 |
|---|---|---|---|
| **M1 骨架** | 端到端跑通空转 Agent | CLI + `Model` 抽象 + `AgentLoop`(FakeModel) + `ToolRegistry` + 基础 `bash`/`read`/`write` | 🟡 进行中 |
| **M2 安全与确认** | 能安全跑命令 | `ApprovalGate` + `SandboxExecutor` + 分层权限配置 | 🔄 进行中（设计文档 + 步骤文档已完成，待编码落地） |
| **M3 上下文与记忆** | 长任务不爆窗口 | `ContextManager` + `Compactor` + `MemoryStore` + prompt caching | ⚪ 待启动 |
| **M4 扩展能力** | 可组合可伸缩 | `SkillLoader` + `SubagentSpawner` | ⚪ 待启动 |
| **M5 生产化** | 可恢复可观测可测 | 韧性层 + 可观测(Trace/Span) + 会话恢复 + 测试金字塔 + CI | ⚪ 待启动 |

## M1 步骤文件

- `milestones/M1-骨架/README.md` — 里程碑计划与步骤索引
- `milestones/M1-骨架/1.1-项目脚手架与Model抽象.md`
- `milestones/M1-骨架/1.2-工具注册与内置工具.md`
- `milestones/M1-骨架/1.3-ReAct循环与事件流.md`
- `milestones/M1-骨架/1.4-PLAN模式.md`
- `milestones/M1-骨架/1.5-意图澄清.md`
- `milestones/M1-骨架/1.6-CLI入口与最简可观测.md`

> M2–M5 启动时，按 `milestones/template.md` 新建 `milestones/Mx-名称/` 并展开步骤文件。

## M2 步骤文件（设计文档 + 多步文档已完成，待编码）

- 设计依据：`knowledge/sandbox-approval-design.md`（Codex 模式：沙箱 + 审批完整设计）
- `milestones/M2-安全与确认/README.md` — 里程碑计划与步骤索引
- `milestones/M2-安全与确认/2.1-沙盒执行层.md`
- `milestones/M2-安全与确认/2.2-审批门.md`
- `milestones/M2-安全与确认/2.3-分层权限配置.md`
- `milestones/M2-安全与确认/2.4-工具与循环集成.md`
- `milestones/M2-安全与确认/2.5-CLI与HITL交互.md`
- `milestones/M2-安全与确认/2.6-测试与验收.md`
