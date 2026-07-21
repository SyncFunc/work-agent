# AGENTS.md — 项目级指令与记忆

> 本文件内容永不压缩（固定底座），用于保存跨对话保留的重要信息。
> 修改后请 commit 到版本控制，使团队成员与新会话都能读取。
> 本项目将 Claude Code 的 `CLAUDE.md` 命名为 `AGENTS.md`（见里程碑 M4.5）。

## 项目简介

一个类似 Claude Code / Codex 的**通用编码 Agent**（Python 3.12+），覆盖 11 项能力：
ReAct 循环、工具调用/注册/沙箱+确认、上下文管理/压缩/长短记忆、Skill 体系/子 Agent
上下文 fork、项目与会话隔离、韧性层（限流/熔断/降级）、意图澄清、配置代码分离、CLI 入口、
可观测性（trace/span 父子）、测试驱动。

## 技术栈

- 语言：Python 3.12+
- CLI：`typer`；异步：`asyncio`；配置：`pydantic-settings` + YAML 分层
- 模型：自抽象 `Model` 接口 + `FakeModel`/`RecordingModel`（测试），底层走 OpenAI 兼容协议
  （`/v1/chat/completions`）。换模型只改 `<项目根>/.agent/settings.yaml` 的 `llm.api_key` /
  `llm.base_url` / `llm.model`，无需改代码。默认值指向 DeepSeek。
- 持久化：`sqlite`（会话/记忆/事件流），路径 `<project>/.agent/`
- 可观测：自研 `Tracer`（OTel 语义，父子 parent_id），可导出 JSON / 接 Langfuse
- 测试：`pytest` + `pytest-asyncio`；LLM 一律可 Mock

## 关键约定

- **开发模式**：里程碑（Milestone）+ 步骤（Step）。每步含「实现方案 / 验收标准 / 知识沉淀」，
  步骤完成后必须向 `milestones/<Mx>/` 与 `knowledge/INDEX.md` 沉淀知识。
- **配置分层**（高→低）：CLI 参数 > 项目级 YAML > 用户级 YAML > 内置默认；不读 `.env`/环境变量。
  密钥 `llm.api_key` 写在 `<项目根>/.agent/settings.yaml`（已被 `.gitignore` 忽略）。
- **上下文管理（M4）**：固定底座（System 静态段 + 本文件 + Tools 列表）永不压缩；
  对话历史按「Microcompact → Session Memory → Auto Compact」四级防线逐级压缩。
- **安全在 OS 层**：沙箱是可插拔执行层（local/docker/external），prompt 仅软约束。
- **能力正交**：Tool（原子）/ Skill（按需包）/ Subagent（隔离上下文）三层。
- **两条全局主线**：事件流（状态单一事实来源）+ Trace/Span（可观测）。
- **git 纪律**：commit / push 前必须获得用户明确同意，不要自动提交或推送。

## 常用命令

```bash
pip install -e ".[dev]"      # 安装（开发）
pytest -q                     # 跑测试
pytest --cov=agent           # 带覆盖
python -m agent.cli chat     # 交互
python -m agent.cli run "..." # 一次性执行
python -m agent.cli resume <session_id>  # 恢复会话
```

## 当前状态

- M1–M5 已完成；M4.5（固定底座与 AGENTS.md 注入）已落地；M4.6（`/context` `/compact`）与
  M4.7（集成测试）待启动。
- 上下文压缩四道防线（Microcompact / Session Memory / Auto Compact / Reactive Compact）均已编码，
  并通过 `ContextManager` 集成进 `Session.step` 与 `AgentLoop.run`。
