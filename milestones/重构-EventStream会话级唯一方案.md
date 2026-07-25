# 重构方案：EventStream 提升为 session 级唯一真相

> 状态：方案评估（待实施）
> 触发：M9 分支排查「回放 seq 重复 / 重复渲染」时发现的根本性设计债
> 关联文档：`M-refactor-统一传输层与事件线格式.md`、`M7-agentrunner守护进程分离/`、`M9-Electron桌面客户端/`

---

## 1. 背景与动机

排查 daemon 回放 `seq` 重复时发现一个比「打补丁」更根本的矛盾：

- **架构主张**（`events.py:1`「状态单一事实来源」、`session.py:98`「冷启动把完整 EventStream 挂到 `session.event_stream`」、`knowledge/INDEX.md`「EventStream 全量不可变，保存/审计/压缩派生源」）都是：**`EventStream` 是 session 唯一、跨轮累积的真相**。
- **实际实现**分裂成两个真相：
  1. `session.event_stream`（`session.py:99`）：只在 `from_store`/resume 时赋值一次（`session.py:245`），`step` 多轮后**从不更新**，内存真相陈旧脱节。
  2. DB 里（被 `SessionStoreSink` 逐条灌入）：才是真正完整的跨轮序列。
- 根因：`loop.run` 每轮新建临时 `EventStream`（`loop.py:146` `stream = EventStream()`），靠 `SessionStoreSink` 把碎片抽出来拼回 DB，还额外用 `_next_seq` 改写 seq（`session_store.py:196-214`）、`_replay` 重编 seq（`server.py:207-217`）来补圆「seq 全局唯一」契约。

**目标**：让 `session.event_stream` 成为跨轮唯一、被 `loop.run` 复用的 stream，从源头消灭 seq 补丁与真相分裂。

---

## 2. 现状事实（已代码核实）

| 项 | 位置 | 现状 |
|---|---|---|
| 主 run 每轮新建 stream | `loop.py:146` | `stream = EventStream()`，seq 从 0 起 |
| session 持有 stream 字段 | `session.py:99` | `self.event_stream: EventStream \| None = None` |
| stream 仅在 resume 赋值 | `session.py:245` | `sess.event_stream = stream`（重建用） |
| step 不更新 stream | `session.py:283` | 只 `self.messages = res.messages` |
| sink 改写全局 seq | `session_store.py:206-214` | `ev.seq = self._seq; self._seq += 1` |
| `_replay` 重编 seq | `server.py:216` | `d["seq"] = i` |
| 子 agent 独立 stream | `subagent.py:299-320` | `loop.run(task, transport=sub_transport, ...)`，无 `stream`/`event_sink` 参数 |
| 后台 agent 独立 stream | `session.py:383` | `spawner.spawn(...)` 同样不传 `stream`/`event_sink` |

`EventStream.append` 的 seq 即 `len(self._events)`（`events.py:189`）——一个 stream **跨多轮 append 天然全局单调递增**，技术上零障碍复用。

---

## 3. 改动目标

1. **session 持有唯一跨轮 `EventStream`**：新建会话初始化空 stream；resume 从 DB 重建并赋值 `self.event_stream`。
2. **`loop.run` 复用外部传入的 stream**（不传则内部新建，保持向后兼容）。
3. **`SessionStoreSink` 删除 `_next_seq` 改写补丁**：落盘直接用 `ev.seq`（已是全局）。
4. **`_replay` 删除重编补丁**（或仅保留防御性断言）。
5. 顺带消除两个衍生问题：实时转发 `seq` 0 起、内存真相脱节。

---

## 4. 涉及文件与接口变更

### 4.1 `agent/core/events.py`
- `EventStream.__init__` 可选增加 `start_seq: int = 0`（当前 `append` 用 `len(self._events)`，跨轮复用无需改；此参数仅用于极端场景，本方案**不一定需要**）。

### 4.2 `agent/core/loop.py`
- `run(...)` 新增可选参数 `stream: EventStream | None = None`：
  - 传了 → 订阅 `transport.bind(stream)` + `event_sink` 到它，`USER`/各事件 `append` 到它；`AgentResult(events=stream, ...)`。
  - 没传 → 内部 `stream = EventStream()`（**现状行为，subagent/后台 agent 走此分支，零变化**）。
- `stream.append(Event(type=EventType.USER, text=task))` 不变（复用同一 stream，seq 自然接续）。

### 4.3 `agent/core/session.py`
- `__init__`：新增会话时 `self.event_stream = EventStream()`（空，seq 0 起）。
- `from_store`/resume：保持 `sess.event_stream = stream`（从 DB 重建，seq 已是全局）。
- `step`：构造 `event_sink = SessionStoreSink(...)` 后，把 `self.event_stream` 作为 `stream=` 传给 `loop.run`；step 结束**不重建** stream（它已累积本轮事件）。
  - 注意：`res.events` 即 `self.event_stream`，`step` 返回后 `res.events` 无需赋回（已是同一对象）。

### 4.4 `agent/context/session_store.py`
- `SessionStoreSink.__call__`：删除 `self._seq`/`_next_seq` 改写逻辑，直接用 `ev.seq` 调 `append_event`。`SessionStore._next_seq` 可保留（fork 等仍可用）但 sink 不再依赖。
- 说明：resume 重建的 stream 经 `from_json` 保留原 seq，`from_json` 后下一个 append 的 seq = `len` = DB 事件数 = 正确下一个全局 seq，无需重算。

### 4.5 `agent/daemon/server.py`
- `_replay`：删除 `d["seq"] = i` 重编（实时/缓冲 seq 已全局）；可保留「按缓冲顺序发送」结构，或改为断言 `seq` 已单调递增做防御。

### 4.6 `agent/daemon/bridge.py`
- **无需改**。bridge 订阅主 run 的 stream（复用 session 级），收到的 `ev.seq` 自动全局唯一 → 实时转发 `seq` 0 起问题**自动消失**，`event_buffer` 内 seq 亦全局唯一。

---

## 5. 对 subagent / 后台 agent 的影响评估（核心问题）

### 结论：✅ 零影响

| 路径 | 是否传 `stream` | 是否传 `event_sink` | 是否碰 `session.event_stream` | 结论 |
|---|---|---|---|---|
| 主 `step`（改后） | 传 `self.event_stream` | 传 `SessionStoreSink` | 是（复用） | 唯一改动点 |
| `subagent.spawn`（`subagent.py:313`） | 否 → 内部新建 | 否 | 否 | **不变** |
| `spawn_background`（`session.py:383`） | 否（经 spawn） | 否 | 否 | **不变** |
| `session-memory` 后台子 agent | 否 | 否 | 否 | **不变** |

**理由**：
1. 子 agent 永远调用 `loop.run(task, transport=sub_transport, ...)`，**不传 `stream` 也不传 `event_sink`** → 走 `loop.run` 的「内部新建独立 stream」默认分支，其事件仅由 `_SubAgentTransport` 消费渲染，`run` 结束后只取 `result.text` 摘要（注入 `self.messages` 或落 `summary.md`）。
2. 子 agent 的事件**不落盘任何 SessionStore、不并入父 session 的 event_stream**（设计铁律：`subagent.py:8-9`「子 agent 拥有独立 EventStream，不混入父 EventStream」）。
3. 只要保持 `loop.run`「无 `stream` 参则内部新建 + `event_sink=None`」的默认行为（4.2 已保证），所有 subagent/后台路径行为完全不变。

### 5.1 子 agent 记录落点与「重复渲染」专项结论

**记录落在哪（三层，已按代码核实）**：
1. **事件流层（EventStream）**：子 agent 的每步 `EventStream` 是 `spawn` 内 `loop.run` 临时新建、随 `AgentResult` 返回后**丢弃**（`subagent.py:313-325`）。它**不落盘任何 SessionStore、不进 `handle.event_buffer`、不参与任何回放**——因为子 agent 的 transport 是 `_SubAgentTransport`/`_SubAgentTuiTransport`（渲染到终端面板/Textual 子区），**不是 `BridgeTransport`**，而只有 `BridgeTransport._on_event` 才会写 `event_buffer` 与实时推送。最终只有 `result.text`（文本摘要）作为主 agent 的一个 **tool_result 事件**经主 session 的 `BridgeTransport` 进入持久化/回放/渲染（`subagent.py:8-9` 铁律「不混入父 EventStream」）。

2. **trace / span 层**：子 agent `loop.run(parent_span=...)` 挂到父 span 下，子 span 被 `Tracer` 收集（可导出 json / Langfuse），desktop 经 `TraceTree` 展示。这是独立于 `EventStream.seq` 的树状可观测数据（OTel `parent_id` 语义），不受本方案/历史 seq bug 影响。

3. **后台 agent 专属**：`session-memory`（记忆子 agent）的 `result.text` 由父 Session 落盘 `summary.md`；其他后台 agent 结果回主上下文或被 `notify`。桌面端 `BackgroundAgents.tsx` **只靠 `notify` 文本正则匹配状态点**（「已启动 / 已完成」，`BackgroundAgents.tsx:17-19`），**不消费子 agent 事件流**——即后台子 agent 运行期的流式输出在桌面端**不可见**（只有两个状态点）。

**会不会有重复渲染问题**：
- 我们修的两类 bug（① reducer 跨轮 `lastDecisionText` 兜底重复；② 回放 `seq` 重复）**都只作用于「主 session EventStream 消费链」**：`BridgeTransport._on_event → event_buffer → 前端 reducer`。子 agent 事件**不在这条链上**，所以**既不会触发、也不会被这两类 bug 影响**。
- 主 session 里子 agent 只表现为**一个 tool 块**（tool_call→tool_result 一次），渲染一次，不重复；递归 spawn 也只是一层层嵌套 tool 块，各渲染一次。
- **实时展示的正确姿势（见 §5.2）**：要把子 agent 实时展示到桌面端，**不要**把它并进父 session 的 `event_buffer` / 复用父 seq；正确做法是给子 agent 一个**独立 subsession**（独立 EventStream + 独立 seq 空间 + 独立转发路由）。这与本方案「EventStream 会话级唯一」一脉相承（泛化为「session/subsession 级唯一」），从根上避免 seq 冲突。
- 后台 agent 的 `notify` 匹配是「尽力而为」（`BackgroundAgents.tsx:43` 按 agent 名匹配最近运行中的任务，完成通知不含 `task_id`），同名并发多个会串——属 `notify` 正则脆弱点，**非渲染重复 bug**。

### 5.2 子 agent 实时展示：独立 subsession（设计扩展）

**需求**：子 agent 过程要实时展示到桌面端，且拥有自己的 subsession（独立会话实体，而非父会话里一个黑盒 tool 块）。

**结论：方向正确，且强化本方案**。要点：
- 把「EventStream 是 session 级唯一真相」**泛化为「session / subsession 级唯一」**——每个 subsession 持有独立 `EventStream`（独立 seq 空间），**不并入父 session 的 `event_buffer`**，因此**不会重演父 session 的 seq 冲突 / 跨轮兜底重复**。
- 子 agent 的 `EventStream` 同样要遵循 Step A 的「跨多轮复用、全局 seq」原则（若子 agent 自身会多轮 run / 嵌套，则复用同一 subsession stream），从根上保证 subsession 内部 seq 唯一——本方案 Step A 为它铺好 seq 基础。

**落地面（非 Step A 范围，列为 Step D 后续，已 grep 确认当前无 subsession 概念）**：
1. **subsession 标识与注册**：子 agent run 时生成 `subsession_id = f"{parent_session_id}/sub_{name}_{depth}_{short_uuid}"`；在 `SessionRegistry` 注册（新增「父→子」索引或独立 namespace）。当前 `SessionHandle`/`registry` 均无 subsession 概念（grep `subsession` 0 结果）。
2. **子 agent transport 改造**：`spawn` 当前用 `_SubAgentTransport`（终端面板，`subagent.py:286`）。实时展示需让子 agent 的 `EventStream` 被一个「subsession 转发 transport」订阅，按 `subsession_id` 路由到 daemon 转发层。可新增 `SubsessionBridgeTransport`（复用 `BridgeTransport` 的序列化映射，但 `_send` 按 `subsession_id` 路由而非父 `attached_conn`）。
3. **daemon 转发路由升级（最大改动）**：当前 `BridgeTransport._send` 只发 `handle.attached_conn`（单连接，`bridge.py:81-87`）。subsession 需：registry 按 `subsession_id` 索引 handle；事件按 `subsession_id` 路由到「订阅该 subsession 的客户端」；前端 `attach` 扩展为可 attach subsession。即转发模型从「单 attached_conn」升级为「按 session/subsession 路由多订阅」。
4. **前端通道与渲染**：`desktop/src/protocol/client.ts` 当前只有 `session.attach` 按 `session_id` 订阅（无 subsession）。需扩展 `attach` 支持 `subsession_id`；`BackgroundAgents.tsx` 从「只显示 notify 状态点」升级为「实时订阅 subsession 事件流并渲染 ToolBlocks / 文本气泡」，可嵌套在主会话 tool 块下或独立 panel。
5. **生命周期 / 持久化**：subsession 是父 session 子资源。建议运行期内存态 + 实时转发即可（子 agent 结果最终回父上下文）；若需重建/回放再落盘 subsession 表，父 session 删除时级联清理，避免 registry 条目膨胀。

**风险**：
- daemon 转发从「单连接」升级为「按 session/subsession 多订阅路由」是较大重构（路由层）。
- 多客户端并发订阅同一 subsession 的顺序/去重：沿用本方案全局 seq 即可。
- 并发多个子 agent → 多个 subsession handle，需生命周期与内存管理。

**已纳入本次重构范围**：Step D 与 Step A 一并实施（见 §7 Step D）。Step A（主路径 session 级唯一）先行、零影响现有 subagent 行为，为 Step D 的 subsession seq 唯一性铺好基础；Step D 在其后落地 subsession 实时展示。

### 并发安全
- 主 `step` 串行；后台 subagent / session-memory 经 `asyncio.ensure_future` 与主 step **并发**（`session.py:415`）。
- 但后台路径用独立 stream、不写 `SessionStoreSink`（不碰 DB 的 events 表）；只有主 step 的 sink 写 DB → **无竞态**。
- session 级 `event_stream` 的 `append` 是单线程同步操作（`events.py:187-196`），主 step 与后台子 agent 各写各的 stream，互不干扰。

---

## 6. 回归风险点（已核查）

1. **测试对 `seq` 绝对值无硬假设**（grep `tests/` 验证）：全部按 `type`/`顺序` 迭代（`next(e for e in res.events if e.type==...)`、`[e.type ...]`、`enumerate`、`to_json/from_json` 比类型序列），无 `seq == 0` 断言。重构后单轮 `res.events` 仍是顺序正确的序列（仅 seq 从 N 起），**不破坏测试**。
2. **`decisions_from_eventstream(res.events)` 需核查**（`cli.py:165`、`test_tape_record_slow.py:47`、`test_tool_tapes.py:90`）：确认其内部仅按类型/顺序遍历、不依赖 `seq` 绝对值；若是则安全。
3. **`res.events` 消费方**：`step` 后只取 `res.messages`（`session.py:283`），`res.events` 当前无强消费方；复用 stream 不影响。
4. **resume 衔接**：`session.event_stream` 从 DB 重建后 seq 已是全局，新一轮 `append` 自然接续 → 比当前 `_next_seq` 更优雅，需补一个单测覆盖「resume 后新事件 seq 接续」。
5. **内存增长（未在本方案根治，见 §7）**：`session.event_stream` 跨轮全量驻留内存 + daemon `event_buffer` 全量累积 → 长会话/后台会话内存随对话线性增长，可能 OOM。

---

## 7. 建议分步实施

### Step A（本方案核心，消灭补丁 + 真相分裂）
- 改 `loop.py`（4.2）、`session.py`（4.3）、`session_store.py`（4.4）、`server.py`（4.5）。
- 验收：`pytest` 全量通过 + 新增「resume 后 seq 接续」「实时/回放 seq 全局唯一」单测。
- 风险：低（subagent 零影响、测试无 seq 绝对假设）。

### Step B（内存风险，建议同 PR 或紧跟）
- `event_buffer` 改为保留最近 K 条（环形/截断），或让 `handle` 引用 `session.event_stream.all()` 的「最近 K 条」视图；解决 §6.5 的 OOM 风险。
- `session.event_stream` 长会话内存：可评估「内存只留最近 N 条 + 旧事件以 DB 为准」，但需保证 `rebuild_messages`/`replay` 仍可得完整序列。

### Step C（回归验证）
- `pytest -q` 全量 + `vitest run` 全量（前端 reducer 不依赖 seq，已确认）+ `tests/integration/test_daemon.py`。
- 手动：多轮对话 → 断开重连 → 确认回放历史连续无重复、seq 单调。

### Step D（子 agent 实时展示：独立 subsession，已纳入本次范围）

> MVP 原则：**复用父 session 的现有 `attached_conn` 做事件多路复用（EVENT 消息带 `subsession_id`），不引入多连接路由重构**，把改动面压到最小；CLI / 非 daemon 模式 subagent 行为完全不变。

**D.1 协议层（`desktop/src/protocol`）**
- `types.ts`：`MsgType.EVENT` 的 payload 增加可选 `subsession_id?: string`；新增 `SubsessionEvent` 类型。`session.attach` 协议不变（前端仍只 attach 父 session）。
- `client.ts`：`onEvent` 回调透传 `subsession_id`；不强制新增 subscribe 接口（子 agent 事件经父连接自带 `subsession_id`，前端自动按 id 建 panel）。

**D.2 daemon 层（`agent/daemon`）**
- `registry.py`：`SessionRegistry` 增加 `_subsessions: dict[str, SessionHandle]` + `register_subsession(parent_id, handle)` / `unregister_subsession(id)` / `get_subsession(id)`；父 `SessionHandle` 增加 `children: set[str]`；父 handle 清理时级联 `unregister_subsession`。
- `bridge.py`：`BridgeTransport._send` 增加 `subsession_id` 形参，转发 EVENT 时 `conn.send(MsgType.EVENT, {"event": ev.to_dict(), "subsession_id": sid}, session=parent_id)`。新增 `SubsessionBridgeTransport(BridgeTransport)`：构造持 `parent_handle`，`bind` 订阅子 agent stream 并写入 `subsession_handle.event_buffer` + 实时转发（自动带 `subsession_id`）；`event_buffer` 改为每 subsession 独立环形缓冲。
- `server.py`：`_replay` 除父 `event_buffer` 外，逐 subsession replay 其 `event_buffer`（每条带 `subsession_id`）；subsession 事件 seq 在各自 subsession 内全局唯一（沿用 Step A 的 session 级唯一原则）。

**D.3 subagent 层（`agent/subagent.py`）**
- `spawn` 增加可选 `parent_handle: SessionHandle | None = None`：
  - 当在 daemon 模式且给定 `parent_handle` → 生成 `subsession_id = f"{parent_handle.session_id}/sub_{spec.name}_{depth}_{short_uuid}"`；创建 `sub_session_stream = EventStream()`（独立 seq 空间）；`register_subsession` 建 handle（`event_buffer` + `SubsessionBridgeTransport(parent_handle, subsession_id)`）；`loop.run(task, transport=sub_transport, stream=sub_session_stream, ...)` **复用 subsession 独立 stream**。
  - 当非 daemon（`parent_handle=None`）→ 保持现有 `_SubAgentTransport` 本地渲染，行为不变（向后兼容）。
  - run 结束后保留 subsession handle（供回放/查询），直到父 session 清理时级联销毁。
- 注意：子 agent 的 `result.text` 仍回父上下文（tool 结果），subsession 事件**不并进父 `event_buffer`**，父 reducer 不被污染。

**D.4 前端层（`desktop/src/features`）**
- 会话 reducer：`useEventReducer` 识别 EVENT 的 `subsession_id`，路由到 `subsessions: Record<subsession_id, Blocks>` 子状态（主会话 reducer 不受影响，避免重复渲染 bug 复发）。
- `BackgroundAgents.tsx`：从「只显示 notify 状态点」升级为「接收 subsession 事件流并实时渲染 ToolBlocks / 文本气泡」；面板可嵌套在主会话对应 tool 块下或独立展开。
- `App.tsx` / Session 视图：挂载 subsession panels（按 `subsession_id` 索引）。

**D.5 验收（Step D）**
- 子 agent 运行时桌面端**实时**看到其文本/工具流（非仅「已启动/已完成」两点）。
- 断开重连后 subsession 历史从 `_replay` 恢复，且 seq 在 subsession 内单调唯一。
- 多个并发子 agent 各 subsession **不串**（按 `subsession_id` 分桶）。
- CLI 模式子 agent 行为不变；`pytest -q` + `vitest run` 全量绿。

---

## 8. 验收标准（Step A）

- [ ] `loop.run` 传 `stream` 时复用之；不传时内部新建（subagent/后台行为不变，补 `test_subagent.py` 断言子 agent 仍独立 stream）。
- [ ] 主 `step` 多轮后 `session.event_stream` 累积全部事件且 `seq` 会话级单调递增（单测）。
- [ ] `SessionStoreSink` 不再改写 seq（`pytest` 中抽查落盘 `seq` 与内存一致）。
- [ ] `_replay` 发送的事件 `seq` 严格递增且等于 `session.event_stream` 内的 seq（单测 `test_daemon.py`）。
- [ ] `pytest -q` 全量绿；`ruff check .` 绿。
- [ ] `decisions_from_eventstream` 路径（`test_tape_record_slow.py`/`test_tool_tapes.py`）仍通过。

---

## 9. 知识沉淀位置

- 完成后写入：`milestones/M9-Electron桌面客户端/M9.4 或 M9.2 知识沉淀` 小节（EventStream 会话级唯一契约 + subagent 独立 stream 铁律）。
- 追加：`knowledge/INDEX.md` 对应小节（「EventStream 是 session 唯一真相，loop.run 复用而非每轮新建」+ 子 agent 永不直接写父 session event_stream）。

---

## 10. 实施记录

### Step A（已完成，已提交并推送）
- commit `249460e`。核心：`loop.run` 复用外部 `stream`；`session` 持有唯一跨轮 `event_stream`；`SessionStoreSink` / `_replay` 删除 seq 改写补丁（见 §5 / §8 验收项）。

### Step B（已完成，待提交）
- 实施要点：
  - `events.py`：`EventStream.__init__` 增加 `maxlen`（运行期内存上限）+ **独立单调 `_seq` 计数器**（与列表长度解耦，截断不影响全局 seq 唯一性，避免 Step A 消灭的 seq 重复 bug 复发）；新增 `EventStream.tail(maxlen)`（保留最近 K 条真实 seq、续写从最大 seq+1 起）。`append` 改用 `_seq` 而非 `len(_events)`；`from_json` 重建时 `_seq = max(seq)+1`。
  - `settings.py`：`ContextConfig.event_stream_maxlen`（默认 4000；`<=0` 表示不限制，兼容旧行为）。
  - `session.py`：新会话 `event_stream = EventStream(maxlen)`；`from_store` 用**完整流** `rebuild_messages` 后，运行期 `event_stream` 封顶为 `stream.tail(maxlen)`（完整历史仍在 sqlite，持久化不受影响）。
- 验收单测：`tests/unit/test_event_stream_session_level.py` 追加 `test_stream_maxlen_trims_but_keeps_global_seq` / `test_stream_tail_preserves_true_seq_and_continues`。
- 全量 `pytest -q`：**443 passed, 0 failed**（Step B 无新增回归）。

### 附：修复工作树 pre-existing 的 3 个测试失败（与 EventStream 重构无关，随 Step B 一并修复）
> 这 3 个失败在 Step A/B 之前就存在于工作树（M9 多项目改造引入），并非本次重构引入。根为明确后一并修复，使全量测试转绿。

1. `tests/unit/test_cli.py::test_chat_skills_lists_and_skill_load` 与 `test_chat_skill_load_appends_message`：
   - **根因**：M9 多项目改造把 `Session.__init__` 的 `cwd` 解析从 `Path(os.environ.get("AGENT_PROJECT_ROOT") or Path.cwd())` 改成纯 `project_root` 参数驱动（未传时回退 `Path.cwd()`），**无意丢失了 `AGENT_PROJECT_ROOT` 环境变量的兜底**。测试用 `monkeypatch.setenv("AGENT_PROJECT_ROOT", tmp_path)` 但 `Session(..., settings)` 未传 `project_root` → `SkillLoader` 扫真实 cwd 而非 tmp_path → `get("demo")` 返回 `None`。
   - **修复**：`Session.__init__` 在 `project_root` 未传时回退 `AGENT_PROJECT_ROOT` 环境变量（再回退 `cwd`）。daemon 路径始终显式传 `project_root`（`server.py:491/496`），参数优先，不受影响。
2. `tests/unit/test_model.py::test_settings_llm_defaults`：
   - **根因**：本机 `~/.agent/settings.yaml`（用户级配置）含真实 `api_key`，`Settings` 读取用户级 YAML 是既定特性；测试只隔离了 `AGENT_PROJECT_ROOT`、未隔离 `AGENT_USER_CONFIG_DIR`，导致 `s.llm.api_key != ""` 断言失败（环境脆弱性，非代码 bug）。
   - **修复**：测试同时隔离 `AGENT_USER_CONFIG_DIR` 与 `AGENT_PROJECT_ROOT` 到不存在的临时目录，真正"无配置"验证默认值。

### Step C（已完成，待提交）
- 全量回归验证（Step B/D 改动面 + pre-existing 修复一并复核）：
  - Python：`pytest -q` → **446 passed, 0 failed, 5 deselected**（443 基线 + 3 个 Step D 新增 `test_subsession.py`）。
  - 前端：`npx vitest run` → **10 files / 49 passed**（含此前 4 个失败的 `client.test.ts`，修复见下）。
  - 类型：`npx tsc --noEmit` 零错误。
  - 质量门：`ruff check .` 全绿（修复 `subagent.py` 的 `SessionHandle/SessionRegistry` 改用 `TYPE_CHECKING` 导入）、`ruff format --check .` 全绿（顺带格式化 `events.py`、两个 pre-existing 未格式化测试文件）。
- 预存前端失败 `desktop/src/protocol/client.test.ts`（4 例）：根因 `FakeWebSocket` 构造未置 `readyState=1`，client 的 `readyState === WS_OPEN` 发送守卫拦截全部消息 → `sentEnvelope(0)` 解析 `undefined`。修复：构造时在 `queueMicrotask` 内 `onopen` 前置 `this.readyState = 1`。

### Step D（子 agent 实时展示：独立 subsession，已完成，待提交）
- 实施要点（MVP：复用父 `attached_conn` 多路复用，不引入多连接路由）：
  - **协议层** `desktop/src/protocol/`：`types.ts` 的 `AgentEvent` 增 `subsession_id?: string | null`；`client.ts` `dispatch` 在 EVENT 分支注入 `ev.subsession_id = msg.payload['subsession_id']` 再派发。
  - **daemon 层** `agent/daemon/`：
    - `registry.py`：`SessionHandle` 增 `parent_id` / `children: set[str]` / `registry`；`SessionRegistry` 增 `_subsessions` + `register_subsession` / `get_subsession` / `unregister_subsession` / `cascade_remove`；`new()`/`restore()` 注入 `handle.registry` 与 `session.daemon_handle`/`session.daemon_registry`。
    - `bridge.py`：新增 `SubsessionBridgeTransport(BridgeTransport)`，构造持 `parent_handle, handle`；覆写 `_send`（经 `parent_handle.attached_conn` 发，session=父 id）与 `_on_event`（写 `handle.event_buffer` 并实时转发 EVENT 带 `subsession_id`）。
    - `server.py`：`_replay` 在父 `event_buffer` 回放后，若 `handle.registry` 存在则遍历 `handle.children` 逐 subsession 回放（每条带 `subsession_id`）。
  - **subagent 层** `agent/subagent.py`：`spawn` 增 `parent_handle` / `registry` 形参；daemon 模式且二者非 `None` 时建 `sub_id = f"{parent_id}/sub_{name}_{depth}_{uuid6}"`、`register_subsession`、`sub_stream = EventStream()`、`sub_transport = SubsessionBridgeTransport(...)`，`loop.run(stream=sub_stream, transport=sub_transport)`；非 daemon 保持 `_SubAgentTransport`/`_SubAgentTuiTransport` 本地渲染（向后兼容）。`spawn` 形参处用 `TYPE_CHECKING` 导入类型，运行时用函数内局部导入 `_SubHandle`。
  - **前端层** `desktop/src/features/obs/BackgroundAgents.tsx`（整文件改写）：订阅 `client.onEvent`，按 `subsession_id` 分桶，每 subsession 经 `buildChatModel(t.events)` + `MessageList` 实时渲染文本/工具流；保留 notify 状态点（RE_START/RE_DONE/RE_BG_LINE）。导入 `buildChatModel`（`../chat/useEventReducer`）与 `type AgentEvent`（`../../protocol/types`）修正此前 TS2459。
- 验收单测：`tests/unit/test_subsession.py` 新增 3 例（`test_subsession_transport_forwards_with_subsession_id` / `test_subsession_registry_links_parent_children` / `test_replay_includes_subsession_events`）。
- 验收（对照 D.5）：`pytest -q` + `vitest run` 全绿；子 agent 事件经父连接实时带 `subsession_id` 到达前端、按 id 分桶互不串；CLI 模式行为不变；`_replay` 恢复 subsession 历史。
- 注意：`subsession` 此前零实现（agent/ 与 desktop/ 全仓 0 命中），本次为从零落地，未与既有 M9 桌面改造冲突。
