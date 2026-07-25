# 里程碑 M9：Electron 桌面客户端

> 基于 agentrunner（即 M7 守护进程 `python -m agent.cli daemon`）构建功能齐全的桌面客户端。
> 内部拉起 daemon 进程，渲染层（React + TypeScript）通过 WebSocket 直连消费 `Event.to_dict()` 事件流，
> 相当于 Textual TUI（M8）的 Web/桌面等价物，**不复用** Python/TerminalTransport 渲染代码。

## 已确认的关键决策（贯穿全里程碑）

| 项 | 决策 |
|---|---|
| 前端技术栈 | **React + TypeScript**（Vite 构建） |
| 代码位置 | 本仓库新增 **`desktop/`** 子目录（与 `agent/` 同仓，便于共享协议/类型契约） |
| 通信架构 | **渲染进程直连 daemon WS**（`ws://127.0.0.1:<port>`）；daemon 仅绑回环，开发期够用。后续如需更强管控可切主进程代理（不在本期） |
| Python 运行时 | **开发期依赖系统已装 Python + `pip install -e .`**；Electron 经 `PATH` 找 `python`/`python3` 启动 daemon。打包分发（PyInstaller 冻结/内置 venv）**后续再定，本期不做** |
| daemon 生命周期 | **全局单一 daemon**：Electron 启动即 spawn 一次；项目在 UI 内切换 |
| 项目模型 | daemon 需改造为**多项目感知**（每个会话绑定 `project_root`，按项目隔离 settings 与 `SessionStore`）——见 M9.0 |
| 功能范围（q4 全选） | ① 多会话标签页 + 会话列表 + resume/fork + 重放 ② 流式 Markdown + 可折叠工具块 + diff 高亮 + 参数/结果分离 ③ HITL 模态 + 审批/沙箱可视化 ④ 设置面板 ⑤ 命令面板 + 斜杠命令 ⑥ 可观测：上下文状态栏 + trace 树 + 日志 + 后台子 agent 状态 |

## 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│ Electron 主进程 (desktop/main)                                 │
│  - spawn: python -m agent.cli daemon  (单次，全局单例)         │
│  - 轮询 http://127.0.0.1:<health_port>/health 直到 200         │
│  - 读取 daemon.port / token 写入 renderer 启动参数             │
│  - 监听 daemon 进程退出 / 应用退出时 kill                       │
└───────────────┬──────────────────────────────────────────────┘
                │ 仅传递 ws url + token（daemon 绑回环，渲染进程直连）
┌───────────────▼──────────────────────────────────────────────┐
│ Electron 渲染进程 (desktop/src, React+TS)                       │
│  - protocol 客户端库 (M9.2)：WS 连接/重连/消息编解码/HITL 配对   │
│  - 各功能面板 (M9.3–M9.7) 消费 Event 流 + 协议消息              │
└───────────────┬──────────────────────────────────────────────┘
                │ WebSocket (M7 协议, agent/daemon/protocol.py)
┌───────────────▼──────────────────────────────────────────────┐
│ agentrunner daemon (agent/daemon, M9.0 多项目感知改造后)         │
│  - 按 project_root 隔离 SessionStore + load_settings            │
│  - 事件流 → Event.to_dict() → WS event 消息                     │
└──────────────────────────────────────────────────────────────┘
```

**协议契约唯一来源**：`agent/daemon/protocol.py`（`MsgType` 枚举 + 信封格式）是事实来源；
`desktop/src/protocol/` 下的 TS 类型须与之保持同步。M9.2 增加一份**契约测试**，用 pytest
断言 TS 类型文件中的 `MsgType` 取值集合与 Python `MsgType` 完全一致，防止漂移。

## 前置依赖

- **M7 已完成**：daemon + WS 协议 + CLI 客户端（多会话切换、replay、HITL 回传均已落地）。M9 直接复用其协议与 `Event.to_dict()` 序列化边界。
- **M8 文档已生成（可选参考）**：Textual 全屏 TUI 的事件→渲染映射（流式 Markdown、可折叠工具块、diff 高亮、HITL 模态、命令面板、状态栏）是桌面端的**功能等价参考**，但代码不复用，仅借鉴交互设计。
- 系统已装 Python 3.12+ 且 `pip install -e ".[dev]"` 完成（开发期前提）。

## 步骤索引

| 步骤 | 文件 | 目标 |
|---|---|---|
| M9.0 | [M9.0-daemon多项目感知.md](./M9.0-daemon多项目感知.md) | **前置**：把 daemon 改造为多项目感知（按 `project_root` 隔离 settings/SessionStore），扩展协议携带 `project_root`；CLI 客户端同步更新 |
| M9.1 | [M9.1-Electron外壳与daemon生命周期.md](./M9.1-Electron外壳与daemon生命周期.md) | `desktop/` 脚手架（Electron+Vite+React+TS）；主进程 spawn/守护/kill 单例 daemon；渲染进程直连 WS 的连接管理 |
| M9.2 | [M9.2-TS协议客户端库.md](./M9.2-TS协议客户端库.md) | TS 协议客户端库（消息编解码、重连、HITL 配对、Event 反序列化）；Python↔TS `MsgType` 契约测试 |
| M9.3 | [M9.3-项目管理与多会话.md](./M9.3-项目管理与多会话.md) | 项目根切换器 + 多会话标签页 + 会话列表 + resume/fork + 重放 |
| M9.4 | [M9.4-流式渲染与工具块.md](./M9.4-流式渲染与工具块.md) | 流式 Markdown + 可折叠工具块 + diff 高亮 + 参数/结果分离 |
| M9.5 | [M9.5-HITL模态与审批可视化.md](./M9.5-HITL模态与审批可视化.md) | HITL 模态（ask/confirm_plan/approve）+ 审批/沙箱可视化 |
| M9.6 | [M9.6-设置面板与命令面板.md](./M9.6-设置面板与命令面板.md) | 设置面板（llm/api_key/base_url/model/plan/theme 等）+ 命令面板 + 斜杠命令 |
| M9.7 | [M9.7-可观测面板.md](./M9.7-可观测面板.md) | 上下文状态栏 + trace 树 + 日志 + 后台子 agent 状态（含 daemon `trace.query` 扩展） |

## 里程碑级知识沉淀

> 本里程碑全部步骤完成后，汇总跨步骤结论（协议契约、daemon 多项目模型、连接生命周期、共享类型策略）。
