# 里程碑总览

> 开发模式详见根目录 `CODEBUDDY.MD`。**每个里程碑一个文件夹 `milestones/<Mx-名称>/`，每步一个 `.md` 文件**，均含「实现方案 / 验收标准 / 知识沉淀」三要素。
> 当前进度：M1 已完成、M2 已完成、M3 已完成、M4 部分完成（M4.1–M4.4 已落地，M4.5–M4.7 待启动）、M5 已完成（5.1–5.5 全编码 + 后台 Agent 介绍，全量 `pytest` 314 passed）、M6 待启动（启动前用 `milestones/template.md` 建文件夹与步骤文件）、M7 待启动（设计期已组织为多步里程碑，待编码落地）。

| 里程碑 | 目标 | 关键交付 | 状态 |
|---|---|---|---|
| **M1 骨架** | 端到端跑通空转 Agent | CLI + `Model` 抽象 + `AgentLoop`(FakeModel) + `ToolRegistry` + 基础 `bash`/`read`/`write` | 🟢 已完成（`AgentLoop`/`Model`/`ToolRegistry`/内置工具/`Session`/`AgentTransport`/`cli` 全部落地，test_model/test_loop/test_registry/test_tools/test_intent/test_plan/test_cli 通过） |
| **M2 安全与确认** | 能安全跑命令 | `ApprovalGate` + `SandboxExecutor` + 分层权限配置 | 🟢 已完成（`sandbox.py`/`approval.py`/`terminal_transport.py` 已编码，test_sandbox/test_approval 通过） |
| **M3 可观测与韧性层** | 看得见、可恢复、不掉线 | Trace/Span 增强 + Span 持久化 + Log 系统 + 韧性层（限流/熔断/降级） + 健康检查 | 🟢 已完成（Tracer 持久化 + 韧性层核心/Pipeline + `agent health` 与 HTTP `/health` 全部编码，test_obs/test_resilience/test_health 通过） |
| **M4 上下文与记忆** | 长任务不爆窗口 | `ContextManager` + Microcompact + Auto Compact(9段摘要) + Session Memory + AGENTS.md 固定底座 + `/context` `/compact` 命令 | 🟡 部分完成（M4.1–M4.4 已落地，test_context 通过；M4.5–M4.7 待启动） |
| **M5 扩展能力** | 可组合可伸缩 | `SkillLoader` + `SubagentSpawner` + CLI 命令 + 测试验收 | 🟢 已完成（Skill 双轨加载/触发目录 + Subagent 内置 explore/plan/general-purpose + 主循环集成/工具白名单 + CLI `/skills`/`/agents`/`/skill`/`/agent`/`/bg` + 全量测试 314 passed） |
| **M6 生产化** | 可恢复可观测可测 | 会话恢复 + 测试金字塔 + CI | ⚪ 待启动 |
| **M7 agentrunner 守护进程分离** | 渲染层与 agentrunner 完全分离为守护进程 + 前端 | daemon + WS 协议 + CLI 客户端 + 多会话切换 | ⚪ 待启动（设计期已组织为多步里程碑） |

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

## 跨里程碑重构文档（standalone，纯设计，不属 M1–M6 步骤）

- `milestones/M-refactor-统一传输层与事件线格式.md` — 双协议合并为 `AgentTransport` + `EventStream` 定为唯一实时线格式 + `ToolRisk` 枚举（已落地）。

> agentrunner 守护进程分离重构已组织为 **M7 里程碑**（见下「M7 步骤文件」），不再以 standalone 文档形式存在。

## M7 步骤文件（设计期已组织为多步里程碑，待编码落地）

- 设计依据：既有传输层重构 `milestones/M-refactor-统一传输层与事件线格式.md` 已落地（`AgentTransport` + `EventStream`）。
- `milestones/M7-agentrunner守护进程分离/README.md` — 里程碑计划与步骤索引（目标 / 前置 / 架构 / 全局约定[含两修复点] / 步骤索引）。
- `milestones/M7-agentrunner守护进程分离/M7.1-daemon骨架.md`
- `milestones/M7-agentrunner守护进程分离/M7.2-协议层.md`
- `milestones/M7-agentrunner守护进程分离/M7.3-CLI客户端.md`
- `milestones/M7-agentrunner守护进程分离/M7.4-session切换回放.md`
- `milestones/M7-agentrunner守护进程分离/M7.5-与run-chat共存.md`
- `milestones/M7-agentrunner守护进程分离/M7.6-安全与端到端验收.md`

## M5 步骤文件（5.1–5.5 已全部编码落地，全量 `pytest` 314 passed）

- 设计依据：`knowledge/claude-code-subagents-skills.md`（Subagents + Skills 机制详细调研，含本项目对接点清单）
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
