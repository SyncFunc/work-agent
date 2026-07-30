# 前端可观测面板优化设计（暂不实现）

> 基于当前 `desktop/src/features/obs/` 的代码分析，提出重构方案。
> 改四件事：① 删除独立日志页，日志归入 span 详情；② span 信息完整展开；③ 美化 trace 展示；④ 按 session 分组的 trace 列表。

---

## 1. 现状分析

### 1.1 当前结构

```
ObsPanel（主容器）
├── StatusBar（token 用量 / 模式 / 会话 id）
├── Tabs（trace / 日志 / 后台）
│   ├── trace → TraceTree（遍历 span 树）
│   │   └── TreeNodeView（每行：name·kind·status·duration·log_count）
│   ├── 日志  → LogView（滚动列表，从 notify 消费）
│   └── 后台  → BackgroundAgents
```

### 1.2 核心问题

| 问题 | 根因 |
|---|---|
| **日志与 span 断层** | `LogView` 从 `notify` 消息消费，是实时控制台流；而 `span.logs` 是 trace 存储的结构化日志。二者数据源不同，用户无法在 trace 里看某一步的详细日志 |
| **span 信息不完整** | `TreeNodeView` 只展示 `name·kind·status·duration·log_count`，`meta`（含 trace_id/message_id/user_text/usage/error_type）完全不可见，logs 只显示条数无内容 |
| **trace 展示简陋** | 纯 inline 样式 + 少量颜色标记，无筛选、无分隔、无视觉层次 |
| **session/trace 切换不直观** | 下拉框就一列 trace_id 缩写，用户分不清属于哪个 session、哪个用户输入 |

---

## 2. 目标设计

### 2.1 页面结构（去掉「日志」tab）

```
Tabs（trace / 后台） ← 删掉「日志」
├── trace → TracePanel（重写 TraceTree）
│   ├── SessionBar（新增：按 session 分组的下拉框）
│   │   └── session 选择 → 底下显示该 session 的所有 trace（二次选择）
│   ├── TraceList（新增：trace 分页列表，每行显示 状态图标 / trace_id / span 数 / 耗时 / user_text）
│   └── SpanTree（TreeNodeView 增强版）
│       ├── 摘要行（name, kind, status, duration, 标签）
│       └── 展开详情（meta 表格 + logs 列表 + 子 span）
└── bg → BackgroundAgents（不变）
```

### 2.2 组件职责

#### 新增/重写的组件

| 组件 | 职责 |
|---|---|
| `TracePanel` | 取代 `TraceTree`，管理 trace 加载和状态 |
| `SessionBar` | 下拉或标签切换 session（展示 session_id 缩写 + span 总数） |
| `TraceList` | 当前 session 下的 trace **分页纵向列表**（状态图标 + trace_id + span 数 + 耗时 + user_text），每页 15 条，带 `◀ 1 2 3 … ▶` 分页栏 |
| `SpanDetail` | 被选中 span 的完整信息面板（meta 键值表格 + logs 列表） |
| `SpanTree` | 增强 `TreeNodeView`，支持展开详情面板 |

#### 被移除的组件

| 组件 | 原因 |
|---|---|
| `LogView` | notify 日志并入 span 详情（`SpanDetail` 里展示当前选中 span 的 `span.logs`） |
| `TraceTree` | 被 `TracePanel` + `SessionBar` + `TraceList` 替代 |

### 2.3 数据流

```
listTraces(projectRoot) → [{trace_id, session_id, span_count, first_ts, last_ts}]
  │
  ├→ SessionBar: 展平 session_id，去重后显示会话列表
  │   └→ 选中 session → 过滤 traces 传给 TraceList（分页列表）
  │
  ├→ TraceList: 展示该 session 下所有 trace（分页列表，每页 15 条），
  │    每行：状态图标 + trace_id + span 数 + 耗时 + user_text
  │   └→ 点击行 → getTrace(trace_id) → spans[]
  │
  └→ SpanTree: 渲染 span 树
      └→ 展开某个 span → SpanDetail: meta 表格 + logs 列表
```

**数据来源说明**：
- `listTraces` 的返回数据里只有 `trace_id / session_id / span_count / first_ts / last_ts`，**没有 `user_text`**。
- 要展示 user_text，有两个方案：
  - **方案 A**：修改后端 `list_traces` 返回 `user_text`（从 spans 的第一行 meta 里提取）。简单，但需要后端修改和协议变更。
  - **方案 B**：前端先 `listTraces` 拿到 trace_id 列表，然后并行 `getTrace(trace_id)` 读取第一条 span 的 meta.user_text。成本高（N 次请求）。
- **推荐方案 A**：在 `TraceStore.list_traces()` 的 GROUP BY 查询中，用 `MIN(started_at)` 关联取第一个 span 的 `meta_json`，从 meta_json 中提取 `$.user_text`。SQLite JSON 函数：`json_extract(MIN(meta_json), '$.user_text')`。需要确认后端支持。

**回退方案**：后端无法快速修改时，先用 `trace_id.slice(0, 8)` 作为展示标签（与当前一致），「美化」阶段再依赖方案 A。

---

## 3. 逐组件设计

### 3.1 `TracePanel`（新组件）

替代 `TraceTree`，是「trace」tab 的渲染入口。

```
props: client, projectRoot
state: sessions[]             // 从 traces 中提取的去重 session 列表
       activeSessionId        // 当前选中的 session
       traces[]               // 当前 session 下的 trace 列表
       activeTraceId          // 当前选中的 trace id
       spans[]                // activeTraceId 对应的 span 树
       selectedSpan           // 用户点开的某个 span 详情
```

**UI 布局**（纵向 flex）：

```
┌─────────────────────────────────────────┐
│ SessionBar ▼                             │  ← 横向
├─────────────────────────────────────────┤
│ TraceList (+ 分页)                        │  ← 分页列表
│ 30 / 120 条  ◀ 1 2 3 … ▶              │
├─────────────────────────────────────────┤
│ SpanTree (左)      │ SpanDetail (右)     │  ← 左右分栏
│                     │                    │    或下方抽屉
└─────────────────────────────────────────┘
```

### 3.2 `SessionBar`（新组件）

```
props: sessions: { session_id, span_count }[]
       active: string
       onChange: (session_id) ⇒ void
```

展示为下拉或标签页：

```
┌──────────────────────────────────┐
│ 📁 会话  #abc123  (45 spans)    │
│       #def456  (12 spans)       │
│       #789ghi  (87 spans)       │
└──────────────────────────────────┘
```

**功能**：
- 从 traces 列表里 `distinct session_id` 构建下拉选项
- 每个选项显示 `session_id.slice(0,8) + span 总数`
- 选中后过滤 trace 列表传给 `TraceSelector`
- 默认选中当前 `ObsPanel.props.sessionId`（由 `TracePanel` 向上找 `RespPanel` 传入）

**为什么要新增 SessionBar**：当前 `TraceTree` 的 `<select>` 直接列出 trace（无 session 分组）。现在 session 可以有多条 trace，必须先选 session 再选 trace。

### 3.3 `TraceList`（新组件，带分页）

```
props: traces: TraceInfo[]       // 当前 session 下所有 trace，后端返回的完整列表
       active: string | null     // 当前选中的 trace_id
       onSelect: (trace_id) ⇒ void
       pageSize: number          // 每页条数，默认 15
```

展示为**分页纵向列表**（而非横向卡片），对长时间会话更友好：

```
┌────────────────────────────────────────────────────┐
│ ✅ #f3a2…  9 spans  12.3ms  sandbox build    ✓    │  ← 当前选中
│ ● #b7c1…  5 spans  45.1ms  refactor utils        │
│ ● #9e1d…  3 spans  102.4ms fix race condition     │  ← 失败 trace
│ …                                                  │
├────────────────────────────────────────────────────┤
│ 30 / 120 条  ◀ 1  2  3  …  8 ▶       每页 15 条   │  ← 分页栏
└────────────────────────────────────────────────────┘
```

**每行**（纵向列表行）：

| 列 | 内容 |
|---|---|
| 状态图标 | `✅` 全部 span 正常结束 & status=ok / `⚠️` 有 warn / `❌` 有 error / `⏳` 有 open |
| trace_id | 首 8 字符，等宽字体 |
| span 数 | `N spans` |
| 耗时 | `last_ts - first_ts`，格式化为 ms/s |
| user_text | 首 30 字符摘要（从 meta 提取），无则显示第一条 span 的 name |
| 选中态 | 蓝色左边框 + 浅蓝底色 |

**分页机制**：
- 前端对 `traces` 数组做切片：`const page = traces.slice((current - 1) * pageSize, current * pageSize)`
- 分页栏显示当前页/总页数，`◀ 1  2  3  …  N ▶` 导航
- 没必要走后端分页（trace 总量有限，单 session 几十~几百条，全量列表已返回）
- 当用户切换 session 时，重置到第 1 页

**交互**：
- 点击行 → `onSelect(trace_id)` → 加载 span 树（高亮该行）
- 右键行 → 复制 trace_id（`navigator.clipboard.writeText(trace_id)`）
- 意外场景：当前选中的 trace 被分页切走了（不在可见页）= 自动选回当前页第一条，或保留选中但滚动到所在页

### 3.4 `SpanTree`（增强 `TreeNodeView`）

在现有 `TreeNodeView` 基础上增加：

**摘要行增强**（一行显示）：

```
▾ agent.run [agent] · ok · 1,230.5ms
│  ├ ▸ model.act [model] · ok · 340.1ms
│  └ ▸ tool.exec [tool] · ok · 890.2ms
│         └ ▸ tool.sandbox [sandbox] · ok · 450.3ms
│               · logs: 3 records · meta: {tool:read, ...}
```

**颜色/图标**：
- `open`：黄色闪烁脉冲点（`⏳`）
- `ok`：绿色圆点（`●`）
- `error`：红色叉号（`✕`）
- 耗时高亮：>1s 用亮色，>5s 用警告色

**点击展开详情**：点击 span 行 → 右侧/下方弹出 `SpanDetail` 面板

### 3.5 `SpanDetail`（新组件）

```
props: span: SpanNode | null
       onClose: () ⇒ void
```

展示被选中 span 的完整信息：

```
┌────────────────────────────────────────────┐
│ Span 详情                         [✕] 关闭 │
├────────────────────────────────────────────┤
│ span_id:        a1b2c3d4                   │
│ name:           tool.exec                  │
│ kind:           tool                        │
│ status:         error                       │
│ parent_id:      e5f6g7h8                   │
│ started_at:     12:34:56.789               │
│ ended_at:       12:34:57.123               │
│ duration:       334.0ms                    │
├────────────────────────────────────────────┤
│ Meta（3 项）                                │
│ ┌─────────────────────────────────────────┐ │
│ │ tool           read                     │ │
│ │ trace_id       f3a2b1c0                │ │
│ │ args           {"path": "/tmp/x"}      │ │
│ └─────────────────────────────────────────┘ │
├────────────────────────────────────────────┤
│ Logs（2 条）     [过滤]                     │
│ ┌─────────────────────────────────────────┐ │
│ │ 12:34:56.801  INFO  tool=read args=...  │ │
│ │ 12:34:57.100  ERROR exec_error=...      │ │
│ └─────────────────────────────────────────┘ │
└────────────────────────────────────────────┘
```

**信息块**：
1. **基本信息表**：`span_id, name, kind, status, parent_id, started_at, ended_at, duration`
2. **Meta 表格**：遍历 `span.meta` 所有 key（排除 `logs` 和子项），`key → value` 渲染。对 JSON 值做格式化缩进展示。
3. **Logs 列表**：当前 span 的 `span.logs`，按 ts 升序。每行：时间 + 级别 + key + value 摘要。
   - 级别颜色：error（红）、warn（黄）、info（默认）、debug（灰）
   - 筛选输入框：按 `key` / `value` / `level` 过滤
4. **子 span 快捷导航**：列出该 span 的直接子 span 名称，点击跳转。

### 3.6 日志归入 span 细节

**数据来源变更**：
- 旧：`LogView` 从 `notify` 消息（`useObs.logs`）消费，是实时控制台消息流
- 新：`SpanDetail.logs` 从 `span.logs`（trace 存储的结构化日志）展示
- 保留：`useObs.logs` 仍可保留作为调试后台，但不再在前端 UI 展示；或改为不可见的 `console.debug` 输出

**展示位置**：
- 当用户点击某个 span（展开 `SpanDetail`），log 列表自动展示该 span 的所有 `span.logs`
- 不在 span 摘要行显示日志数量（视觉噪音），在 `SpanDetail` 里看

### 3.7 移除 LogView

**需删除的文件**：
- `desktop/src/features/obs/LogView.tsx`
- CSS 类 `.wa-logview*`（`layout.css` 中）

**Tab 修改**：
- `ObsPanel.tsx` 的 `TABS` 数组从 `['trace', 'log', 'bg']` 改为 `['trace', 'bg']`
- `type Tab` 从 `'trace' | 'log' | 'bg'` 改为 `'trace' | 'bg'`
- `Tabs` 元素的 `label` 可加 tooltip：`Trace` → `Trace(span详情)`
- 移除 LogView 的引用和渲染

---

## 4. CSS 样式变更

新增/修改的 CSS 类都在 `layout.css` 中。

### 4.1 新增 `wa-sessionbar`
```css
.wa-sessionbar {
  display: flex;
  align-items: center;
  gap: var(--wa-s2);
  padding: var(--wa-s2) var(--wa-s3);
  border-bottom: 1px solid var(--wa-border);
  font-size: var(--wa-f-sm);
}
.wa-sessionbar select {
  flex: 1;
  font-size: var(--wa-f-sm);
  background: var(--wa-bg-subtle);
  border: 1px solid var(--wa-border);
  border-radius: var(--wa-r-md);
  padding: 2px var(--wa-s2);
  color: var(--wa-text);
}
```

### 4.2 新增 `wa-tracelist`（分页纵向列表）
```css
.wa-tracelist {
  flex: none;
  border-bottom: 1px solid var(--wa-border);
  overflow-y: hidden;
  display: flex;
  flex-direction: column;
}
.wa-trace-row {
  display: grid;
  grid-template-columns: 24px 120px 80px 100px 1fr;
  align-items: center;
  gap: var(--wa-s2);
  padding: 4px var(--wa-s3);
  cursor: pointer;
  font-size: var(--wa-f-sm);
  border-left: 3px solid transparent;
  transition: background var(--wa-dur-fast);
}
.wa-trace-row:hover {
  background: var(--wa-bg-subtle);
}
.wa-trace-row--active {
  background: var(--wa-bg-active);
  border-left-color: var(--wa-primary);
}
.wa-trace-row__id {
  font-family: ui-monospace, monospace;
  color: var(--wa-text-muted);
  font-size: var(--wa-f-xs);
}
.wa-trace-row__spans {
  color: var(--wa-text-faint);
  font-size: var(--wa-f-xs);
}
.wa-trace-row__duration {
  color: var(--wa-text-faint);
  font-size: var(--wa-f-xs);
  text-align: right;
}
.wa-trace-row__text {
  color: var(--wa-text);
  font-size: var(--wa-f-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wa-tracelist__pagination {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--wa-s1);
  padding: 4px var(--wa-s3);
  border-top: 1px solid var(--wa-border);
  font-size: var(--wa-f-xs);
  color: var(--wa-text-muted);
}
.wa-tracelist__page-btn {
  padding: 0 6px;
  border: 1px solid var(--wa-border);
  border-radius: var(--wa-r-sm);
  background: var(--wa-bg-subtle);
  cursor: pointer;
  font-size: var(--wa-f-xs);
  color: var(--wa-text);
}
.wa-tracelist__page-btn:disabled {
  opacity: 0.4;
  cursor: default;
}
.wa-tracelist__page-btn--active {
  background: var(--wa-primary);
  color: var(--wa-bg);
  border-color: var(--wa-primary);
}
```

### 4.3 新增 `wa-spandetail`
```css
.wa-spandetail {
  border-left: 1px solid var(--wa-border);
  background: var(--wa-bg);
  width: 320px;
  min-width: 280px;
  overflow-y: auto;
  font-size: var(--wa-f-sm);
}
.wa-spandetail__header {
  display: flex;
  align-items: center;
  gap: var(--wa-s2);
  padding: var(--wa-s2);
  border-bottom: 1px solid var(--wa-border);
  font-weight: 600;
}
.wa-spandetail__close {
  margin-left: auto;
}
.wa-spandetail__section {
  padding: var(--wa-s2) var(--wa-s3);
  border-bottom: 1px solid var(--wa-border);
}
.wa-spandetail__section-title {
  font-size: var(--wa-f-xs);
  color: var(--wa-text-muted);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: var(--wa-s1);
}
.wa-spandetail__field {
  display: flex;
  padding: 1px 0;
}
.wa-spandetail__key {
  color: var(--wa-text-muted);
  min-width: 80px;
  flex: none;
}
.wa-spandetail__val {
  color: var(--wa-text);
  word-break: break-all;
}
.wa-spandetail__meta-row {
  display: flex;
  padding: 1px 0;
  border-bottom: 1px solid var(--wa-border);
}
.wa-spandetail__meta-key {
  color: var(--wa-text-muted);
  min-width: 80px;
  font-family: ui-monospace, monospace;
  font-size: var(--wa-f-xs);
}
.wa-spandetail__meta-val {
  color: var(--wa-text);
  font-family: ui-monospace, monospace;
  font-size: var(--wa-f-xs);
  word-break: break-all;
  white-space: pre-wrap;
}
.wa-spandetail__log-row {
  display: flex;
  gap: var(--wa-s2);
  padding: 1px 0;
  font-family: ui-monospace, monospace;
  font-size: var(--wa-f-xs);
}
.wa-spandetail__log-time {
  color: var(--wa-text-faint);
  flex: none;
}
.wa-spandetail__log-level--error { color: var(--wa-danger); }
.wa-spandetail__log-level--warn  { color: var(--wa-warn); }
.wa-spandetail__log-level--info  { color: var(--wa-text-muted); }
.wa-spandetail__log-level--debug { color: var(--wa-text-faint); }
.wa-spandetail__log-msg {
  word-break: break-word;
}
.wa-spandetail__log-search {
  width: 100%;
  border: 1px solid var(--wa-border);
  border-radius: var(--wa-r-md);
  background: var(--wa-bg-subtle);
  color: var(--wa-text);
  padding: 2px var(--wa-s2);
  font-size: var(--wa-f-xs);
  margin-bottom: var(--wa-s1);
}
```

### 4.4 删除 `wa-logview*` 全部类

### 4.5 微调现有 `wa-obs__body`

当前 `wa-obs__body` 只有 `flex: 1; overflow: hidden; min-height: 0;`，结构适配后不需要变动。`TracePanel` 内部自己管理横向布局（`SpanTree + SpanDetail`）。

---

## 5. 状态管理变更

### 5.1 `useObs` 不变

`useObs` 的 `logs`（notify 消息）可保留但不再展示。改为：
- `clearLogs` 变为 noop
- `logs` 数组在 UI 上不可见
- 或者彻底移除，但需确认无人依赖 `obs.logs`

**建议**：暂不删除 `useObs.logs`，保留但不展示，留给未来控制台功能复用。

### 5.2 `TracePanel` 状态

```typescript
interface TracePanelState {
  sessions: string[]                    // 去重后的 session_id 列表
  activeSessionId: string | null
  traces: TraceInfo[]                   // 当前 session 下的 trace
  activeTraceId: string | null
  spans: SpanNode[]                     // 当前已加载的 span 树
  selectedSpanId: string | null         // 在 SpanDetail 中展开的 span
  loading: boolean
  error: string | null
  traceLoading: boolean                 // 请求 trace 树的加载态
}
```

### 5.3 请求策略

- 挂载时：`listTraces(projectRoot, sessionId)` → 填充分组状态
- session 切换时：filter traces（不需要重新请求，已有全部数据）
- trace 切换时：`getTrace(projectRoot, traceId)` → 刷新 spans + selectedSpanId = null
- refresh 按钮：重新 `listTraces`

---

## 6. 文件变更清单

| 操作 | 文件 |
|---|---|
| **创建** | `TracePanel.tsx`（新入口，替代 TraceTree） |
| **创建** | `SessionBar.tsx` |
| **创建** | `TraceList.tsx`（分页纵向列表，替代横向卡片方案） |
| **创建** | `SpanDetail.tsx` |
| **删除** | `TraceTree.tsx`（被 TracePanel 替代） |
| **删除** | `LogView.tsx` |
| **修改** | `ObsPanel.tsx`（删 log tab，引用 TracePanel 替代 TraceTree） |
| **修改** | `layout.css`（增 sessionbar/tracelist/spandetail css，删 logview css） |
| **可选** | `useObs.ts`（logs 保留但不使用） |
| **可选** | `types.ts`（如果后端需要新增 user_text 字段） |
| **可选** | `store.py`（如果后端 list_traces 需要返回 user_text） |

---

## 7. 实施步骤建议

| 步骤 | 内容 | 估计 |
|---|---|---|
| 1 | 新建 `TracePanel`，复制 `TraceTree` 逻辑但增加多 session 分组 | ~1h |
| 2 | 抽 `SessionBar` 组件，实现 session 切换过滤 | ~0.5h |
| 3 | 抽 `TraceList`（分页组件）：纵向列表 + 分页栏（`◀ 1 2 3 … ▶`） | ~1.5h |
| 4 | 抽 `SpanDetail` 组件，展示 meta 表格 + logs 列表 | ~1.5h |
| 5 | 改造 `TreeNodeView` 支持点击选中 + 状态图标（✅/⚠️/❌/⏳） | ~0.5h |
| 6 | 修改 `ObsPanel`：删 log tab + 换用 `TracePanel` | ~0.5h |
| 7 | 删除 `LogView.tsx` + `layout.css` 中 `wa-logview*` 样式 | ~0.2h |
| 8 | 新增 CSS 类（sessionbar / tracelist / spandetail） | ~0.5h |
| 9 | 端到端测试 + 调整样式细节 | ~1h |

**总估计**：约 7~8 小时。

---

## 8. 开放问题

1. **后端 `list_traces` 是否需要新增 `user_text` 字段？** 如果要做 trace 卡片显示用户输入摘要，需要。否则 trace 卡片只能显示 `trace_id` 缩写 + span 数。
2. **`span.logs` 的数据内容是否足够？** 当前 `SpanLogHandler` 将 `logging` 调用转换为 `{"msg": ..., "module": ..., "line": ..., "func": ..., "extra": ...}`。前端 SpanDetail 的 logs 列表直接渲染这些字段即可，不需要后端改动。
3. **是否保留 `useObs.logs`（notify 消息）作为调试控制台？** 建议保留但不展示，不影响前端构建大小（数组在内存中），未来可复用。
4. **分页方案选择**：后端全量返回 + 前端切片分页，对单 session 几千条 trace 规模够用（每条 trace 仅 30 字节摘要）。若未来量级超过万级，可加入后端 `OFFSET/LIMIT` + 总条数统计接口。
