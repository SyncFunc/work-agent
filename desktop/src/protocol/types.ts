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
  'notify',
  'usage',
  'close',
  'error',
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

export type EventTypeStr =
  | 'decision'
  | 'clarify'
  | 'plan'
  | 'plan_progress'
  | 'tool_use'
  | 'tool_result'
  | 'final'
  | 'error'
  | 'text'
  | 'tool_call_delta'
  | 'user'

/** 一条事件（即 Event.to_dict 的强类型投影）。这是 M9.4 渲染的数据源。 */
export interface AgentEvent {
  seq: number
  type: EventTypeStr
  ts: number
  transient?: boolean
  decision?: Decision | null
  tool_use?: ToolCall | null
  tool_result?: ToolResult | null
  tool_call_id?: string | null
  tc_index?: number | null
  tc_name?: string | null
  tc_args?: string | null
  text?: string | null
  kind?: string | null
  error?: string | null
  questions?: Question[] | null
  plan_path?: string | null
  plan_update?: PlanUpdate | null
}

/** 会话列表项（对齐 daemon registry.list_info 的响应：键为 `id`）。 */
export interface SessionInfo {
  id: string
  name?: string | null
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

/** 一条 trace 摘要（trace.list 响应中的一项；trace_id == session_id）。 */
export interface TraceInfo {
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
  status: 'open' | 'ok'
  meta: Record<string, unknown>
  logs: SpanLog[]
}

/** trace.get 的响应 payload（trace_tree 消息）。 */
export interface TraceTreeResponse {
  session_id: string | null
  spans: SpanNode[]
}

/** usage 消息 payload（对齐 agent/daemon/bridge.py 的 report_usage）。 */
export interface UsagePayload {
  usage: {
    prompt_tokens?: number
    completion_tokens?: number
    total_tokens?: number
    estimated_tokens?: number
  }
  estimated: boolean
}

/** notify 消息 payload。 */
export interface NotifyPayload {
  message: string
}
