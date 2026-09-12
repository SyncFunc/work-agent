# v1.1.0 Release Notes

## 概述

v1.1.0 是本项目里程碑 **M11（技能智能体与 MCP 接入）** 的功能发版，核心能力：

- **接入 MCP（Model Context Protocol）**：让 work-agent 能直接调用外部工具生态（GitHub、数据库、内部 API 等），对标 Claude Code / Codex 的 MCP 支持。
- **工具延迟加载 / Tool Search**：未激活的 MCP 工具不占用模型上下文，需要时按需激活。
- **内建技能 + 内建天气 MCP**：自带 `skillcreator` 技能与真实天气查询工具。
- **天气城市不再硬编码**：任意城市直接查询 wttr.in，支持中文/拼音/英文。

---

## 新功能

### 1. MCP 接入（M11.6）

- 新增 `agent/mcp/` 模块：stdio JSON-RPC 客户端、分层 yaml 配置、适配器、生命周期管理、内置 demo server。
- **配置分层**：用户级 `~/.agent/mcp.yaml` + 项目级 `.agent/mcp.yaml`，项目覆盖用户；支持 `${VAR}` 环境变量展开，密钥不进版本控制。
- **统一命名**：`mcp__server__tool` 三段式；风险等级 fail-closed（只读词才判 read，其余走审批）。
- **无会话也可查询/管理**：daemon 新增 `show_mcp` / `mcp.update` 协议消息（MsgType 46）。
- **前端 MCP 面板**：新增 `McpPanel`，支持 source 徽标（builtin / user / project）、增删改启停、内建只读展示。

### 2. 延迟加载 / tool_search

- 借鉴 Claude Tool Search：未激活的 MCP 工具**不进 tools 列表**（避免模型直接调空 schema 工具），只在 system prompt 文本目录列出。
- 模型调用 `tool_search(query)` 命中即激活，下一轮以完整 schema 进入工具列表。

### 3. 内建技能 + 天气 MCP

- **内建技能 `skillcreator`**：教 Agent 编写规范的 `SKILL.md`。
- **内建天气 MCP**：真实查询 wttr.in，支持中文 / 拼音 / 英文城市输入。
- **天气城市不设白名单、不硬编码**：任意城市直接查询 wttr.in；拼音表仅用于把英文/拼音显示成中文（含绍兴、宁波等）。
- 修复 Windows 子进程 UTF-8 编码乱码问题。

---

## 版本统一

- 版本号统一升至 **v1.1.0**：`pyproject.toml` / `agent.__version__` / `DAEMON_VERSION` / `desktop/package.json`（含 lockfile）同步。
- 前端 `hello` 版本改从统一来源 `shared/version.ts` 读取，去除硬编码漂移。

---

## 文档

- 新增 `docs/mcp-接入设计.md`（MCP 接入的当前设计，含 mermaid 图）
- 新增 `docs/mcp-调研与教程.md`（MCP 调研与上手教程）
- 新增 `milestones/M11-技能智能体与MCP接入/M11.6-MCP接入.md`
- 同步更新 `docs/daemon-api.md`、`knowledge/INDEX.md`

---

## 质量门禁

- 后端：`ruff` ✓ / `basedpyright` ✓ / `pytest` **483 passed** ✓
- 前端：`tsc --noEmit` ✓ / `electron-vite build` ✓

---

## 升级说明

- 后端无需迁移；前端 `hello` 版本号现从 `package.json` 统一读取。
- MCP 配置采用 yaml 分层；如需使用内建天气查询，无需任何配置。
- 新协议消息类型见 `agent/daemon/protocol.py` 与 `docs/daemon-api.md`。
