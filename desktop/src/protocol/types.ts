// 与 agent/daemon/protocol.py 的 MsgType 完全对齐。
//
// 单一事实来源 = ALL_MSG_TYPES（运行时集合）；MsgType 联合类型由它派生，
// 改协议只需改 ALL_MSG_TYPES 一处。契约测试 scripts/check-msgtype.mjs 比对
// Python 端 MsgType 枚举与这里的集合，漂移即失败。

export const ALL_MSG_TYPES = [
  // ---- Client -> Server ----
  'hello',
  'session.new',
  'session.attach',
  'session.switch',
  'session.detach',
  'session.list',
  'task.send',
  'answer',
  'confirm_plan',
  'approve',
  'command',
  'task.cancel', // M9.9 真实中断当前生成
  'session.delete', // M9.9 彻底删除会话（含事件/记忆/trace）
  'session.title', // M11.6 手动设置会话标题 {title}
  'skill.update', // M11.6 技能开关 {name, enabled}
  'agent.update', // M11.6 编辑智能体配置 {name, updates}
  'trace.list',
  'trace.get',
  // ---- Server -> Client ----
  'welcome',
  'session.created',
  'attached',
  'detached',
  'session_list',
  'event',
  'replay_start',
  'replay_end',
  'ask',
  'show_questions',
  'show_plan',
  'show_skills',
  'show_agents',
  'show_tools', // M11.6 已注册真实工具清单：tools 数组，每项含 name/risk/description
  'notify',
  'close',
  'error',
  'task.cancelled', // M9.9 已停止生成
  'session.info', // M9.9 推送 plan_mode / model
  'session.delete_resp', // M9.9 删除结果
  'session.title_resp', // M11.6 标题设置结果 {ok, title?, error?}
  'skill.update_resp', // M11.6 技能开关结果 {ok, name?, enabled?, error?}
  'agent.update_resp', // M11.6 智能体编辑结果 {ok, name?, error?}
  'trace_list',
  'trace_tree',
] as const

export type MsgType = (typeof ALL_MSG_TYPES)[number]

/** 协议信封（JSON 收发）。与 protocol.py 的 make_message/parse_message 一致。 */
export interface Envelope {
  type: MsgType
  id?: string
  session?: string
  payload: Record<string, unknown>
}

// --------------------------------------------------------------------------- //
// Event 家族（对齐 agent/core/events.py 的 Event.to_dict 字段）
// --------------------------------------------------------------------------- //

export interface ToolCall {
  id: string
  name: string
  arguments: Record<string, unknown>
}

export interface Decision {
  text?: string | null
  tool_calls: ToolCall[]
}

export interface ToolResult {
  ok: boolean
  output?: string | null
  error?: string | null
  /** write/edit 等工具的 unified-diff 文本，供 UI 展示改动。 */
  diff?: string | null
  /** 写/改操作前的原始文件内容，供前端自行计算实时 diff。 */
  original?: string | null
}

export interface Question {
  question: string
  options?: string[] | null
  multiSelect?: boolean
}

export interface PlanUpdate {
  step_id: string
  status: string
  note?: string | null
}

/** 一个计划步骤（前端渲染用，对齐后端 PlanStore 步骤投影）。 */
export interface PlanStepView {
  id: string
  title: string
  status: string
}

export type EventTypeStr =
  | 'decision'
  | 'clarify'
  | 'plan'
  | 'plan_progress'
  | 'tool_use'
  | 'tool_result'
  | 'file_original'
  | 'final'
  | 'error'
  | 'text'
  | 'tool_call_delta'
  | 'user'
  | 'usage'

/** 一条事件（即 Event.to_dict 的强类型投影）。这是 M9.4 渲染的数据源。 */
export interface AgentEvent {
  seq: number
  type: EventTypeStr
  ts: number
  /** M9 subsession：归属的子会话 id（顶层会话事件为 undefined/空）。 */
  subsession_id?: string | null
  /** M11.6：事件归属的顶层会话 id（client 派发时从信封 session 注入），
      用于切换会话后过滤旧会话仍在输出的实时事件，避免串渲染。 */
  session_id?: string
  /** M11：后台 subsession 标记（如 session-memory 记忆子 agent），前端据此不渲染进前台聊天区。 */
  background?: boolean
  transient?: boolean
  decision?: Decision | null
  tool_use?: ToolCall | null
  tool_result?: ToolResult | null
  tool_call_id?: string | null
  tc_index?: number | null
  tc_name?: string | null
  tc_args?: string | null
  /** file_original 瞬时事件：write/edit 流式预读的原文件内容。 */
  file_path?: string | null
  file_original?: string | null
  text?: string | null
  kind?: string | null
  error?: string | null
  questions?: Question[] | null
  plan_path?: string | null
  plan_update?: PlanUpdate | null
  /** PLAN / PLAN_PROGRESS 事件携带的完整步骤列表（对齐后端 Event.plan_steps）。 */
  plan_steps?: PlanStepView[] | null
  /** M10.3：USAGE 事件的 usage 字典（token 用量，经 event 消息承载）。 */
  usage?: UsageEvent['usage'] | null
  /** M10.3：本事件归属的 message（逐 message 归集用量）。 */
  message_id?: string | null
  /** M10.3：父 message（子 agent 用量归集回派生子 agent 的 message）。 */
  parent_message_id?: string | null
  /** M10.3：USAGE 事件：无真实用量时为 true。 */
  estimated?: boolean | null
  /** M10.3：USAGE 事件：本次响应墙钟耗时（秒，含 HITL 等待）。 */
  duration?: number | null
}

/** 会话列表项（对齐 daemon registry.list_info 的响应：键为 `id`）。 */
export interface SessionInfo {
  id: string
  name?: string | null
  /** M11.6 显示标题（首个提问 / session memory / 用户手动，持久化）。 */
  title?: string | null
  project_root?: string
  attached?: boolean
  running?: boolean
  /** 内存会话为最后活跃时间戳；持久化会话为 SessionStore 的 updated_at。 */
  last_activity?: number | null
  persisted?: boolean
}

/** session.list 的响应 payload（session_list 消息）。 */
export interface SessionListResponse {
  project_root: string
  sessions: SessionInfo[]
}

// --------------------------------------------------------------------------- //
// 可观测面板（M9.7）：trace 查询（对齐 agent/daemon/server.py 的 _trace_*）
// --------------------------------------------------------------------------- //

/** 一条 trace 摘要（trace.list 响应中的一项；trace_id = message_id，不再等于 session_id）。 */
export interface TraceInfo {
  trace_id: string
  session_id: string
  span_count: number
  first_ts: number | null
  last_ts: number | null
}

/** trace.list 的响应 payload（trace_list 消息）。 */
export interface TraceListResponse {
  project_root: string
  traces: TraceInfo[]
}

/** span 内的一条结构化日志（对齐 agent/obs/tracer.py 的 LogEntry）。 */
export interface SpanLog {
  ts: number
  key: string
  value: unknown
  level: string
}

/** 一个 span 节点（对齐 server.py 的 _span_to_dict）。 */
export interface SpanNode {
  span_id: string
  name: string
  kind: string
  parent_id: string | null
  started_at: number
  ended_at: number | null
  status: 'open' | 'ok' | 'error'
  meta: Record<string, unknown>
  logs: SpanLog[]
}

/** trace.get 的响应 payload（trace_tree 消息）。 */
export interface TraceTreeResponse {
  trace_id: string
  session_id: string | null
  spans: SpanNode[]
}

/** USAGE 事件 payload（对齐 M10.3 EventType.USAGE：message 体含 usage/estimated/duration/message_id/parent_message_id）。 */
export interface UsageEvent {
  usage: {
    prompt_tokens?: number
    completion_tokens?: number
    total_tokens?: number
    reasoning_tokens?: number
    /** 缓存命中（本请求从上下文缓存读取的 token）。 */
    cache_hit_tokens?: number
    /** 缓存未命中（需重新计算输入的 token）。 */
    cache_miss_tokens?: number
    /** 缓存写入（本请求新写入上下文缓存的 token）。 */
    cache_write_tokens?: number
    /** 估算 token（无真实用量时由后端粗估）。 */
    estimated_tokens?: number
  }
  estimated: boolean
  /** 本次响应墙钟耗时（秒，含 HITL 等待）。 */
  duration?: number | null
  /** 归属 message（逐 message 归集用量）。 */
  message_id?: string | null
  /** 父 message（子 agent 用量归集回派生子 agent 的 message）。 */
  parent_message_id?: string | null
}

/** notify 消息 payload。 */
export interface NotifyPayload {
  message: string
}

/** 一个已注册真实工具（show_tools 响应项，M11.6）。 */
export interface ToolInfo {
  name: string
  risk: 'read' | 'edit' | 'exec' | string
  description: string
}
