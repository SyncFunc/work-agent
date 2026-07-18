# 上下文管理设计（Context Management）

> 独立设计文档，配套 `knowledge/INDEX.md` 的「架构决策·上下文稀缺」条目。
> 主题：工具结果（tool_result）到底「保存」还是「临时注入」？结合 Claude Code / Codex 调研，给出本项目（work-agent）的结论与落地路线。
> 状态：M1 阶段为**设计结论 + 第 0 层已落地**；完整压缩层属 **M3 上下文管理里程碑**。

---

## 1. 核心结论：工具结果 = 对话历史（既保存、也注入）

「保存」 vs 「临时注入」是一个**伪二选一**。Claude Code 与 Codex 的实践一致：

- 工具结果就是**对话历史（messages）的一部分**，`tool_result` 与对应的 `tool_use` **配对**出现。
- 同一份数据同时承担两种角色：
  - **持久化 transcript**：落盘、可 resume、可回放 —— 这是「保存」。
  - **每轮喂给模型的上下文**：API 要求 `tool_result` 必须紧跟其 `tool_use`，否则直接 400 报错 —— 所以它天然就在历史里、逐轮注入。

> **API 硬约束**：带 `tool_calls` 的 `assistant` 消息，必须紧跟每个 `tool_call_id` 对应的 `tool` 回执。因此不能「只删结果不删调用」，也不能让结果游离于历史之外。

**结论**：不要另建一个独立的「工具结果存储」与上下文重复。工具结果应作为**对话历史的一员**存在；所谓「保存」交给不可变审计流，「注入」交给可派生的上下文窗口投影（见 §2）。

---

## 2. 本项目双轨映射（CODEBUDDY.md 已定调）

项目已有「事件流（单一事实来源）+ 上下文窗口（派生投影）」双轨，正好对应「保存 / 注入」：

| 表示 | 角色 | 生命周期 | 是否压缩 |
|---|---|---|---|
| `EventStream`（事件流，`agent/core/events.py`） | 状态单一事实来源；审计 / 回放 / trace / **压缩派生源** | **永久、完整**保留原始 `tool_result`（`Event(type="tool_result", tool_result=...)`，见 `loop.py:243`） | **不压缩**（审计真相，不可变） |
| `conv` / `Session.messages`（上下文窗口投影） | 逐轮喂给模型的历史 | 随轮累积；`loop.py:242-246` 把 `Message(role="tool", content=res.output, tool_call_id=tc.id)` 进 `conv` | **未来可压缩**（M3） |

关键点：
- **保存**交给事件流：全量、不可变，是压缩与恢复的唯一真相源。
- **注入**交给 `conv`：从会话状态派生，是未来压缩层的作用对象。
- 二者**不重复存结果内容**，`conv` 是会话历史的当前投影；事件流用于回放重建与派生压缩。

---

## 3. 已落地的「第 0 层」（M1 已有，先防撑爆）

在完整压缩层（M3）之前，M1 已内置三道低成本防护，避免大输出直接撑爆上下文：

1. **输出截断（集中入口）**：`Settings.max_tool_output_chars`（默认 20000），`ToolRegistry.run` 经模块级 `_cap_result` 截断超长 `output`/`error` 并附 `[output truncated: N chars, kept first M]`。事件流与回填 messages 中输出一致被截断（M1.2）。
2. **分页读 + 定位搜**：`read` 支持 `offset/limit` 带行号分页、输出 `lines A-B of TOTAL`；`grep` 单文件正则带行号返回。闭环「grep 定位行号 → read 精确读范围」，避免一次读大文件（M1.2）。
3. **结果不在 assistant 文本里**：assistant 只回传 `tool_calls`，结果在回填阶段以 `role="tool"` 注入，模型不会把整段输出当自己的文本重复生成。

---

## 4. 压缩策略（调研 Claude Code / Codex）

### 4.1 Claude Code：渐进降级防线

| 层级 | 名称 | 成本 | 做法 |
|---|---|---|---|
| 1 | Microcompact | 0 | 把**旧的** tool_result 内容替换为占位符 `[Old tool result content cleared]`；保留最近 N 个不动；消息条数不变；绝不拆散配对 |
| 2 | Snip / Collapse（实验） | 0 | 物理删中间片段 / 折叠为摘要 |
| 3a | Session Memory Compact | 0 | 复用预存摘要替代历史 |
| 3b | AI 摘要压缩 | 1 次调用 | LLM 生成 9 段结构化摘要（意图/技术概念/文件变更/错误修复/待办/当前工作/下一步…） |
| 4 | Reactive Compact | 1 次调用 | API 返 413 时紧急重试 |

- **触发**：接近上限自动触发（默认 200K 窗口，阈值≈有效窗口−13K；1M 窗口类推）。
- **防漂移**：压缩后**主动重新读取最近操作的文件最新版**贴回上下文，确保不「忘记」正在编辑的代码。
- **边界标记**：Compact Boundary 记录压缩点，下次只从边界后取消息。
- **固定底座**：CLAUDE.md / 自动记忆 / 技能体从磁盘重新注入，永不进摘要（对抗长期衰减）。

### 4.2 Codex：ContextManager + 精细压缩

- `ContextManager.items: Vec<ResponseItem>`，含 `ToolCall` / `ToolResult` 枚举，统一 token 计量（`calculate_tool_result_bytes`）。同一份 `items` 既序列化持久化（`SessionPersistence`，支持 fork 分叉），又投影进模型上下文。
- **触发**：窗口填充 80% 触发、95% 强制（Critical）。
- **压缩算法**：
  - `IntelligentCompactor`：LLM（低温 0.3）生成摘要，验证保留 ≥70% 关键实体，否则报错。
  - `LayeredCompressor`：按压力选 `Light/Medium/Heavy/Extreme`。
  - `ProgressiveCompressor`：按 Token 压力压缩最旧 20% / 40%，或仅保留最近 10 条，前方插摘要。
  - `RollingWindow`：分块（≤2048 Token/块）、带重叠与 LRU 缓存。

### 4.3 本项目借鉴（M3 落地建议）

- **先做 Microcompact（0 成本）**：操作 `conv` 中较旧的 `role="tool"` 消息，把 `content` 替换为占位符，保留最近 N 个；不动 `tool_use`/`tool_result` 配对。
- **再做 Auto Compact（1 次调用）**：越阈值时把边界前历史整批压成结构化摘要，插入 Compact Boundary；压缩后重读最近文件最新版防漂移。
- **计量与触发**：复用现有 `_estimate_tokens`（CJK 1 token/字、其它 4 字符/token）估算 `conv` 占用；阈值取「有效窗口 − 余量」。
- 所有压缩**只作用于 `conv`/上下文投影**，**绝不触碰 `EventStream`**（审计真相不可变）。

---

## 5. 配对约束（铁律）

裁剪 / 压缩 / 摘要时，**永远把 `tool_use` 与 `tool_result` 当成一个单元**处理：
- 删调用必删结果，删结果必删调用，或二者一起折叠为摘要；
- 占位替换只改 `tool_result.content`，保留 `tool_call_id` 配对；
- 违反即触发 API 400（已有前车之鉴：M1.5 澄清/计划提前返回缺 tool 回执）。

---

## 6. 大输出隔离：子代理（M4）

读大文件 / 长 bash 日志等超大输出：**不塞主上下文**，委托给**子代理**（M4 subagent，隔离上下文窗口），主窗口只回收「摘要 + 小元数据尾迹」。这与「能力正交：Tool/Skill/Subagent 三层」「Subagent 隔离上下文」的决策一致。

---

## 7. Token 计量与预算（已有基础）

- `_estimate_tokens`（`agent/cli.py`）已提供粗估；M3 应在 `ContextManager` 类组件里对 `conv` 做精确/启发式计量，输出分类明细（系统提示 / 动态对话 / 工具结果）。
- 预算阈值驱动压缩触发；`report_usage` 已打印真实 usage（M1.6），可作校准。

---

## 8. 后续里程碑规划（M3 上下文管理）

待办清单（落到 `agent/context/`）：
1. `ContextManager`：持有 `conv` 投影、估算 token、记录压缩历史。
2. Microcompact：旧 `tool_result` 占位替换 + 最近 N 保留（成对处理）。
3. Auto Compact：阈值触发 → 结构化摘要 + Compact Boundary；压缩后重读最近文件防漂移。
4. prompt caching：稳定前缀（system / CLAUDE.md 类）走缓存，降成本。
5. 子代理隔离接入（M4 协同）：大输出不外溢主窗口。
6. 恢复（M5）：从 `EventStream` 重放重建 `conv`，压缩层可基于事件流重新派生。

**落点纪律**：压缩只改 `conv`（可投影）；`EventStream` 永久全量、只读。

---

## 9. 来源

- Claude Code 官方文档《Explore the context window》（code.claude.com/docs/en/context-window）：自动加载项、文件读取/工具结果进上下文、compaction 触发与存活规则。
- 《CLAUDE CODE 上下文管理机制深度解析》（blog.cyeam.com，2026-06-03）：messages 累积、Microcompact 占位符、Auto Compact 9 段摘要、配对约束、防漂移。
- 《Codex CLI Deep Dive · 第 7 章 Context 管理》（book.cuiliang.ai）：ContextManager.items、token 计量、Intelligent/Layered/Progressive/RollingWindow 压缩、80%/95% 触发。
- 本项目：`CODEBUDDY.md`（双轨/上下文稀缺/能力正交）、`agent/core/events.py`（EventStream）、`agent/core/loop.py:242-246`（tool_result 进 conv）、`agent/runtime/registry.py`（`_cap_result` / `max_tool_output_chars`）、`agent/tools/fs.py`（`read` 分页 + `grep`）。
