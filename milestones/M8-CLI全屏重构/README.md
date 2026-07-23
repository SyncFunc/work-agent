# 里程碑：M8 CLI 全屏重构（Textual 全屏 TUI，对标 Claude Code）

> 设计依据：`knowledge/调研-Textual全屏CLI重构方案.md`
> 模式：里程碑 + 步骤（方案 / 验收 / 知识沉淀）。本里程碑**仅新增**一个与 `TerminalTransport`
> 平级的 `AgentTransport` 实现（`TextualTransport`）+ 一个全屏 Textual `App`，
> 旧 `run`/`chat`/`client` 全程保留，验证充分后再迁移（见调研 §7）。

## 目标

用 Textual 构建对标 Claude Code 的全屏 chat TUI（流式 Markdown / 可折叠工具块 / diff 高亮 /
HITL 模态 / 命令面板 / 多主题 / 鼠标），同时**不改动** `AgentTransport` 协议、`EventStream`、
loop / session / core、daemon 协议；旧 CLI 保留，先并行、后迁移。

## 前置依赖

- **M7（agentrunner 守护进程分离）已完成**：EventStream 订阅范式、回放缓冲稳定（`pytest` 380 passed）。
- `AgentTransport` 协议与 `TerminalTransport` 已实现，并被 `run`/`chat`/`client` 使用。
- 项目已依赖 `rich`（Textual 同团队，零摩擦）；`pytest` + `pytest-asyncio` 已配；`cli.py` 用 `typer`。
- 参考：`调研-agent-cli渲染与runner交互.md`（解耦铁律）、`调研-CLI美化方案.md`（主题/高亮诉求，本里程碑一并满足）。

## 步骤索引

| 步骤 | 文件 | 目标 |
|---|---|---|
| M8.0 | [M8.0-依赖与骨架.md](./M8.0-依赖与骨架.md) | 加 `tui` 依赖 + 空 `ChatApp` 能 `run_test` + `chat --tui` 可启动 |
| M8.1 | [M8.1-TextualTransport事件桥接.md](./M8.1-TextualTransport事件桥接.md) | `TextualTransport` 订阅事件 → UI 部件 |
| M8.2 | [M8.2-输入与chat循环.md](./M8.2-输入与chat循环.md) | `TextArea` 输入 + thread worker 跑 `session.step` |
| M8.3 | [M8.3-HITL模态.md](./M8.3-HITL模态.md) | `ask`/`approve`/`confirm_plan` 模态 + 线程安全 `Future` |
| M8.4 | [M8.4-流式工具块diff高亮.md](./M8.4-流式工具块diff高亮.md) | 流式 Markdown 节流 + `Collapsible` + `Syntax(diff)` |
| M8.5 | [M8.5-主题命令面板状态栏.md](./M8.5-主题命令面板状态栏.md) | TCSS 主题 + `CommandPalette` + `Header` ctx% |
| M8.6 | [M8.6-子agent渲染接入与验收.md](./M8.6-子agent渲染接入与验收.md) | `_SubAgentTuiTransport` + 全量 pytest / 真 TTY 验收 |
| M8.7 | [M8.7-迁移切换.md](./M8.7-迁移切换.md) | 默认 `chat` 切 Textual + `--legacy` 保留旧 |

## 里程碑级知识沉淀

> 待 M8.0–M8.7 全部完成后汇总：Textual 集成范式（thread worker + `call_from_thread` + 线程安全
> `Future`）、`TextualTransport` 完整签名、事件→部件映射表、与旧 `TerminalTransport` 对照、
> 真 TTY 对标清单结果、迁移切换点。同步追加到 `knowledge/INDEX.md`。
