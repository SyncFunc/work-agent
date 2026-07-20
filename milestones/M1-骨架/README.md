# M1 骨架 — 端到端跑通空转 Agent

> 目标：在没有真实 LLM、没有安全/压缩/扩展能力的前提下，先打通"CLI → AgentLoop → ToolRegistry → 工具执行"的端到端骨架，用 `FakeModel` 驱动循环。
> 前置依赖：无。
> 每步细节见同目录下的 `1.x-*.md`，均含「实现方案 / 验收标准 / 知识沉淀」。

> **状态：🟢 已完成** —— `AgentLoop`/`Model`/`ToolRegistry`/内置工具(`bash`/`read`/`write`/`edit`/`grep`)/`Session`/`AgentTransport`/`cli`(run/chat) 全部落地，对应测试（test_model/test_loop/test_registry/test_tools/test_intent/test_plan/test_cli）通过。

## 步骤索引

| 步骤 | 文件 | 目标 |
|---|---|---|
| M1.1 | [1.1-项目脚手架与Model抽象.md](./1.1-项目脚手架与Model抽象.md) | 可安装 + `Model` 抽象 + `FakeModel`/`RecordingModel` + 分层配置（接 DeepSeek） |
| M1.2 | [1.2-工具注册与内置工具.md](./1.2-工具注册与内置工具.md) | `ToolRegistry` + `bash`/`read`/`write` + 单测 |
| M1.3 | [1.3-ReAct循环与事件流.md](./1.3-ReAct循环与事件流.md) | `AgentLoop` 接 FakeModel 跑通空转 + 事件流 |
| M1.4 | [1.4-PLAN模式.md](./1.4-PLAN模式.md) | PLAN 模式：只读探索 + `present_plan` 控制工具 + 计划审批 |
| M1.5 | [1.5-意图澄清.md](./1.5-意图澄清.md) | 意图澄清：模糊任务先 `ask_clarification` 收集答案再执行 |
| M1.6 | [1.6-CLI入口与最简可观测.md](./1.6-CLI入口与最简可观测.md) | typer `run`/`chat` + 占位 trace/span（串联 PLAN/澄清） |

## 里程碑级知识沉淀
> 四步完成后汇总：接口最终签名、模块边界、踩坑、对 M2 的约束（如 ToolResult 形态、Event 字段需 M2 审批复用）。
