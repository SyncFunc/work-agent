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
  output?: string
  error?: string
}

export interface Question {
  question: string
  options?: string[] | null
  multiSelect?: boolean
}

export interface PlanUpdate {
  step_id: string
  status: string
  note?: string
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

/** 会话列表项（对齐 daemon registry.list_info 的响应）。 */
export interface SessionInfo {
  session_id: string
  name?: string | null
  project_root?: string
  created_at?: number
  updated_at?: number
  persisted?: boolean
}
