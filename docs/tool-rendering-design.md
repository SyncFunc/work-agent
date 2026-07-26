# 工具渲染差异化设计文档

## 1. 目标

根据工具类型提供差异化的 UI 渲染，替代当前统一的 `Wrench` 图标 + JSON 参数 + 文本结果的卡片样式。

## 2. 分类方案

| 工具名 | 渲染策略 | 关键行为 |
|--------|---------|---------|
| `bash` | 终端块 | 标题行显示 `Terminal` 图标 + `$ {command}`，内容区显示结果，深色终端风 |
| `write` / `edit` | 纯 diff 块 | 运行中显示 spinner+"写入中…"，完成后自动折叠，高度固定 300px |
| `read` / `grep` | 通用块 | 标题显示工具名 + 文件路径 + 行范围（args.offset-limit），默认折叠，不展示参数区 |
| `present_plan` | 呼吸动画块 | 独立卡片，脉冲/呼吸 CSS 动画 + "正在生成计划…" 文字；不展示参数/结果 |
| `update_plan` / （读计划） | 步骤列表块 | 解析结果文本中的步骤标记，展示带状态图标（完成✓ / 进行中⟳ / 待办○）的列表 |
| `bash` | 终端块 | 标题行显示 `Terminal` 图标 + `$ {command}`，结果区 terminal 风格（跟随主题色） |
| 其他工具 | 通用块（保持不变） | 默认展开，参数 + 结果 |

## 3. 改动清单

### 3.1 `desktop/src/features/chat/ToolBlock.tsx`

**当前**：单一 `ToolBlock` 组件，全部工具同一渲染。

**改为**：分派架构——`ToolBlock` 内根据 `block.name` 路由到专用子组件：

- **`BashBlock`**：`div.wa-tool.wa-tool-bash`
  - `.wa-tool-bash__titlebar`：flex row，`Terminal` 图标，`$ ...` 命令，运行/完成/失败状态，折叠开关
  - 背景/文字色跟随 CSS 变量（`--wa-bg-elevated`、`--wa-text`、`--wa-code-bg`），不硬编码颜色
  - `.wa-tool-bash__output`：等宽字体 `pre`，`--wa-code-bg` 背景
- **`PlanGeneratingBlock`**：`div.wa-tool.wa-plan-gen`
  - `.wa-plan-gen__pulse`：CSS 呼吸动画圆圈
  - `.wa-plan-gen__text`："正在生成计划…" 文字
- **`DiffBlock`**（write/edit）：`div.wa-tool.wa-tool-diff`
  - 头部：`Wrench` 图标 + `write`/`edit` + 文件路径（`args.path`），右侧状态指示
    - 运行中：`Spinner` + "写入中…"
    - 完成：绿色 `CheckCircle2`（无文字），块自动折叠
    - 失败：红色 `XCircle`
  - 正文：固定高度 300px，overflow-y: auto，直接渲染 `DiffView` 或 `pre`
  - 完成后折叠，用户可手动展开查看 diff
- **`ReadBlock`**（read）：`div.wa-tool.wa-tool-read`
  - 头部：`FileText` 图标 + `read {path}` + 行范围（`args.offset`~`args.limit`）
  - 默认折叠（`expanded=false`），不展示参数区
  - 正文展开后只渲染结果（diff 或纯文本），无「结果」标签
- **`GenericToolBlock`**：原 `ToolBlock` 逻辑提取，接受 `defaultCollapsed` prop
  - `grep`：`defaultCollapsed=true`
  - 其他工具：默认展开，参数+结果标准两段式

### 3.2 `desktop/src/features/chat/useEventReducer.ts`

**PlanBlock** 接口新增 `body?: string` 字段，存储计划文本内容，供步骤列表渲染使用。

在 `plan` / `plan_progress` case 中，新增 `body: ev.text ?? undefined`。

### 3.3 `desktop/src/features/chat/MessageItem.tsx`

**计划块渲染**（`case 'plan'`）：

- 保持 `wa-alert wa-alert--plan` 警示块作为容器
- 标题显示 `计划 · {status}`
- 若 `block.body` 存在，解析为步骤列表：
  - 每行检测状态标记：`[x]` / `✓` / `✅` → 完成（strikethrough + CheckCircle2 图标）
  - `[*]` / `⟳` / 运行中 → 进行中（Spinner 图标）
  - `[ ]` / `○` → 待办（Circle 图标）
  - 无标记行 → 纯文本行
- 步骤列表在 `.wa-plan-steps` 容器内，子项 `.wa-plan-step`

### 3.4 `desktop/src/features/chat/chat.css`

新增 CSS 类：

```css
/* bash 终端块 */
.wa-tool-bash { /* 覆盖默认边框和背景 */ }
.wa-tool-bash__titlebar { /* flex row 背景跟随 --wa-bg-elevated */ }
.wa-tool-bash__cmd { /* 等宽，颜色跟随 --wa-text */ }
.wa-tool-bash__output { /* --wa-code-bg 背景，--wa-text 文字，terminal 风格 */ }
.wa-tool-bash__ok { /* 使用 --wa-success */ }
.wa-tool-bash__fail { /* 使用 --wa-danger */ }

/* 生成计划呼吸动画 */
.wa-plan-gen { /* 居中卡片，flex column */ }
.wa-plan-gen__pulse { /* 圆形，呼吸动画 */ }
.wa-plan-gen__text { /* 动画文字 */ }

/* diff 编辑块 */
.wa-tool-diff__body { /* 固定高度 300px，overflow-y: auto，避免流式写入时布局跳动 */ }

/* 计划步骤列表 */
.wa-plan-steps { /* margin-top */ }
.wa-plan-step { /* flex row, gap */ }
.wa-plan-step--done { /* text-decoration: line-through, opacity */ }
.wa-plan-step--active { /* font-weight: 600 */ }
```

## 4. 不变行为

- 工具的 **HITL 审批弹窗**（`present_plan` 的批准/拒绝）：保持不变，由现有 ApprovalDialog 处理
- 写入/编辑文件的 **diff 着色**：保持现有 `DiffView` + `isDiffLike` 检测
- **子 agent 块**（`SubagentCard`）：不受本次改动影响
- **消息/响应组架构**（`ResponseBlock`、`TurnMeta`）：不变

## 5. 验收标准

- [ ] `bash` 工具：标题显示 `$ {cmd}`，输出区终端风格（颜色跟随主题 CSS 变量）
- [ ] `present_plan`：生成时显示脉冲呼吸动画，无参数/结果展示
- [ ] `read`：标题显示 `read {path}` + 行范围，默认折叠，无参数区
- [ ] `write`/`edit`：运行中 spinner + "写入中…"，完成后自动折叠，高度固定 300px，直接渲染 diff
- [ ] 计划块（`PLAN`/`PLAN_PROGRESS`）：状态标记行解析为图标+文字列表
- [ ] 其他工具：与现有行为一致
- [ ] `tsc --noEmit` 通过
