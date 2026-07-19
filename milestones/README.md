# 里程碑总览

> 开发模式详见根目录 `CODEBUDDY.MD`。**每个里程碑一个文件夹 `milestones/<Mx-名称>/`，每步一个 `.md` 文件**，均含「实现方案 / 验收标准 / 知识沉淀」三要素。
> 当前进度：M1 进行中，M2 进行中，M3 已完成，M4 进行中，M5 文档已完成（待编码落地），M6 待启动（启动前用 `milestones/template.md` 建文件夹与步骤文件）。

| 里程碑 | 目标 | 关键交付 | 状态 |
|---|---|---|---|
| **M1 骨架** | 端到端跑通空转 Agent | CLI + `Model` 抽象 + `AgentLoop`(FakeModel) + `ToolRegistry` + 基础 `bash`/`read`/`write` | 🟡 进行中 |
| **M2 安全与确认** | 能安全跑命令 | `ApprovalGate` + `SandboxExecutor` + 分层权限配置 | 🔄 进行中（设计文档 + 步骤文档已完成，待编码落地） |
| **M3 可观测与韧性层** | 看得见、可恢复、不掉线 | Trace/Span 增强 + Span 持久化 + Log 系统 + 韧性层（限流/熔断/降级） + 健康检查 | 🟡 进行中（M3.1–M3.3 代码已完成，M3.4–M3.5 待启动） |
| **M4 上下文与记忆** | 长任务不爆窗口 | `ContextManager` + Microcompact + Auto Compact(9段摘要) + Session Memory + AGENTS.md 固定底座 + `/context` `/compact` 命令 | 🟡 进行中（M4.1–M4.3 已落地，M4.4–M4.7 待启动） |
| **M5 扩展能力** | 可组合可伸缩 | `SkillLoader` + `SubagentSpawner` | 🟡 文档完成（待编码落地） |
| **M6 生产化** | 可恢复可观测可测 | 会话恢复 + 测试金字塔 + CI | ⚪ 待启动 |

## M1 步骤文件

- `milestones/M1-骨架/README.md` — 里程碑计划与步骤索引
- `milestones/M1-骨架/1.1-项目脚手架与Model抽象.md`
- `milestones/M1-骨架/1.2-工具注册与内置工具.md`
- `milestones/M1-骨架/1.3-ReAct循环与事件流.md`
- `milestones/M1-骨架/1.4-PLAN模式.md`
- `milestones/M1-骨架/1.5-意图澄清.md`
- `milestones/M1-骨架/1.6-CLI入口与最简可观测.md`

> M2–M6 启动时，按 `milestones/template.md` 新建 `milestones/Mx-名称/` 并展开步骤文件。
> M4 步骤文档已完成（采用 Claude Code 四层压缩防线方案），待编码落地。

## M5 步骤文件（步骤文档已完成，待编码落地）

- 设计依据：`knowledge/claude-code-subagents-skills.md`（Claude Code Subagents + Skills 机制详细调研，含本项目对接点清单）
- `milestones/M5-扩展能力/README.md` — 里程碑计划与步骤索引
- `milestones/M5-扩展能力/5.1-SkillLoader基础.md`
- `milestones/M5-扩展能力/5.2-SubagentSpawner.md`
- `milestones/M5-扩展能力/5.3-集成与工具白名单.md`
- `milestones/M5-扩展能力/5.4-CLI命令.md`
- `milestones/M5-扩展能力/5.5-测试与验收.md`

## M4 步骤文件（步骤文档已完成，待编码落地）

- 设计依据：`knowledge/claude-code-context-management.md`（Claude Code 四层压缩防线详细调研）、`knowledge/context-management.md`（双轨映射与设计结论，已按 Claude Code 方案修正）
- `milestones/M4-上下文与记忆/README.md` — 里程碑计划与步骤索引
- `milestones/M4-上下文与记忆/4.1-ContextManager基础.md`
- `milestones/M4-上下文与记忆/4.2-Microcompact.md`
- `milestones/M4-上下文与记忆/4.3-AutoCompact.md`
- `milestones/M4-上下文与记忆/4.4-SessionMemoryCompact.md`
- `milestones/M4-上下文与记忆/4.5-集成与固定底座.md`
- `milestones/M4-上下文与记忆/4.6-CLI命令.md`
- `milestones/M4-上下文与记忆/4.7-测试与验收.md`

## M2 步骤文件（设计文档 + 多步文档已完成，待编码）

- 设计依据：`knowledge/sandbox-approval-design.md`（Codex 模式：沙箱 + 审批完整设计）
- `milestones/M2-安全与确认/README.md` — 里程碑计划与步骤索引
- `milestones/M2-安全与确认/2.1-沙盒执行层.md`
- `milestones/M2-安全与确认/2.2-审批门.md`
- `milestones/M2-安全与确认/2.3-分层权限配置.md`
- `milestones/M2-安全与确认/2.4-工具与循环集成.md`
- `milestones/M2-安全与确认/2.5-CLI与HITL交互.md`
- `milestones/M2-安全与确认/2.6-测试与验收.md`
