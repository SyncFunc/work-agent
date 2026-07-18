# 里程碑 M2：安全与确认

> 目标：**能安全跑命令**。为 Agent 接上 OS 级沙箱执行层（Codex 模式）与 AskForApproval 四模式审批门，做到"命令跑在隔离里、危险动作先问人"。
> 设计依据：[`../../knowledge/sandbox-approval-design.md`](../../knowledge/sandbox-approval-design.md)（Codex 模式的沙盒 + 审批完整设计：原理 / profile / 四模式 / 决策流）。
> 开发模式：每步一个 `.md`，含「实现方案 / 验收标准 / 知识沉淀」三要素（见 `../template.md`）。

## 前置依赖

- **M1 全部完成**：`AgentLoop`（`agent/core/loop.py`）、`ToolRegistry`（`agent/runtime/registry.py`，含 `RISK_LEVELS`/`ToolResult`/`ToolSpec`）、内置 `bash`/`read`/`write`/`edit`/`grep` 工具、`Settings` 分层配置、`Session`/`SessionUI`（`agent/core/session.py`）、`_TyperUI`（`agent/cli.py`）。
- 既有**不可破坏**约束：`PLAN` 模式风险门控（`loop._risk_blocked`）保持不动；`ToolResult` 失败降级不崩循环机制保留；输出截断 `_cap_result` 保留；测试不依赖真实 LLM/root/网络。

## 步骤索引

| 步骤 | 文件 | 目标 |
|---|---|---|
| M2.1 | [2.1-沙盒执行层.md](./2.1-沙盒执行层.md) | `SandboxExecutor` 抽象 + 三档 profile + Local/Docker/External + 工厂 |
| M2.2 | [2.2-审批门.md](./2.2-审批门.md) | `ApprovalGate`：四模式 + allow/deny 规则 + HITL 回调 |
| M2.3 | [2.3-分层权限配置.md](./2.3-分层权限配置.md) | `Settings` 增 sandbox/approval 字段 + YAML 声明式 |
| M2.4 | [2.4-工具与循环集成.md](./2.4-工具与循环集成.md) | `bash`→`SandboxExecutor`；`loop` 接入 `ApprovalGate`；`session` 构建 gate |
| M2.5 | [2.5-CLI与HITL交互.md](./2.5-CLI与HITL交互.md) | `SessionUI.approve` + `_TyperUI` 实现 + `run`/`chat` 接入 |
| M2.6 | [2.6-测试与验收.md](./2.6-测试与验收.md) | `test_sandbox` / `test_approval` / 集成回归 |

## 里程碑级知识沉淀

> 全部步骤完成后汇总：沙箱/审批的最终接口、OS 降级策略、与 PLAN 模式的边界、踩坑（Windows 隔离、HITL 异步、deny 优先）。

## 设计取舍速记（来自设计文档）

  - **沙箱 = 可插拔执行层（Codex 模式）**：`local`（Linux landlock+seccomp / macOS Seatbelt / 原生 Windows 应用层 `CommandFilter` 主动拦截·不打印告警）· `docker` · `external`（直通）。三档 profile：`read-only` / `workspace-write` / `danger-full`，**网络默认拒绝**。
  - **审批 = AskForApproval 四模式**：`untrusted`（exec/edit 每步问）· `on-request`（自动跑，模型对单条命令标 `approval_request` 才问）· `on-failure`（失败才问）· `never`（全自动，deny 仍生效）。**`deny` 规则永远优先**于模式（安全不变量）。
- **不破坏 PLAN**：`ApprovalGate` 仅 EXEC 模式介入；PLAN 仍只放行 `read`。
