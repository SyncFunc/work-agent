// Event.from_dict 的 TS 等价：parseEvent(dict) 重建强类型 AgentEvent。
// 字段映射严格对齐 agent/core/events.py 的 to_dict / from_dict（见 M9.2 知识沉淀）。

import type {
  AgentEvent,
  Decision,
  EventTypeStr,
  PlanUpdate,
  Question,
  ToolCall,
  ToolResult,
} from './types'

const EVENT_TYPES: readonly EventTypeStr[] = [
  'decision',
  'clarify',
  'plan',
  'plan_progress',
  'tool_use',
  'tool_result',
  'final',
  'error',
  'text',
  'tool_call_delta',
  'user',
]

function asOptString(v: unknown): string | null | undefined {
  if (v === null || v === undefined) return v as null
  return typeof v === 'string' ? v : String(v)
}

function asOptNumber(v: unknown): number | null | undefined {
  if (v === null || v === undefined) return v as null
  return typeof v === 'number' ? v : Number(v)
}

function asBool(v: unknown): boolean {
  return v === true || v === 'true' || v === 1
}

function parseToolCall(v: unknown): ToolCall | null {
  if (!v || typeof v !== 'object') return null
  const d = v as Record<string, unknown>
  if (typeof d.id !== 'string' || typeof d.name !== 'string') return null
  return {
    id: d.id,
    name: d.name,
    arguments:
      d.arguments && typeof d.arguments === 'object'
        ? (d.arguments as Record<string, unknown>)
        : {},
  }
}

function parseDecision(v: unknown): Decision | null {
  if (!v || typeof v !== 'object') return null
  const d = v as Record<string, unknown>
  const rawTc = Array.isArray(d.tool_calls) ? d.tool_calls : []
  return {
    text: asOptString(d.text),
    tool_calls: rawTc.map(parseToolCall).filter((t): t is ToolCall => t !== null),
  }
}

function parseToolResult(v: unknown): ToolResult | null {
  if (!v || typeof v !== 'object') return null
  const d = v as Record<string, unknown>
  if (typeof d.ok !== 'boolean') return null
  return {
    ok: d.ok,
    output: asOptString(d.output),
    error: asOptString(d.error),
  }
}

function parseQuestion(v: unknown): Question | null {
  if (!v || typeof v !== 'object') return null
  const d = v as Record<string, unknown>
  if (typeof d.question !== 'string') return null
  return {
    question: d.question,
    options: Array.isArray(d.options)
      ? (d.options.filter((o) => typeof o === 'string') as string[])
      : (d.options as string[] | null | undefined),
    multiSelect: typeof d.multiSelect === 'boolean' ? d.multiSelect : asBool(d.multiSelect),
  }
}

function parsePlanUpdate(v: unknown): PlanUpdate | null {
  if (!v || typeof v !== 'object') return null
  const d = v as Record<string, unknown>
  if (typeof d.step_id !== 'string' || typeof d.status !== 'string') return null
  return { step_id: d.step_id, status: d.status, note: asOptString(d.note) }
}

/** 把任意 JSON 解析为强类型 AgentEvent；非法输入抛出 TypeError。 */
export function parseEvent(raw: unknown): AgentEvent {
  if (!raw || typeof raw !== 'object') {
    throw new TypeError('Event 必须是对象')
  }
  const d = raw as Record<string, unknown>
  if (typeof d.seq !== 'number' || typeof d.type !== 'string') {
    throw new TypeError('Event 缺少 seq/type 字段')
  }
  if (!EVENT_TYPES.includes(d.type as EventTypeStr)) {
    throw new TypeError(`未知 Event.type: ${String(d.type)}`)
  }
  const type = d.type as EventTypeStr

  let questions: Question[] | null | undefined
  if (d.questions === undefined) {
    questions = undefined
  } else if (Array.isArray(d.questions)) {
    questions = d.questions.map(parseQuestion).filter((q): q is Question => q !== null)
  } else {
    questions = null
  }

  return {
    seq: d.seq,
    type,
    ts: typeof d.ts === 'number' ? d.ts : 0,
    transient: typeof d.transient === 'boolean' ? d.transient : undefined,
    decision: d.decision === undefined ? undefined : parseDecision(d.decision),
    tool_use: d.tool_use === undefined ? undefined : parseToolCall(d.tool_use),
    tool_result: d.tool_result === undefined ? undefined : parseToolResult(d.tool_result),
    tool_call_id: asOptString(d.tool_call_id),
    tc_index: asOptNumber(d.tc_index),
    tc_name: asOptString(d.tc_name),
    tc_args: asOptString(d.tc_args),
    text: asOptString(d.text),
    kind: asOptString(d.kind),
    error: asOptString(d.error),
    questions,
    plan_path: asOptString(d.plan_path),
    plan_update: d.plan_update === undefined ? undefined : parsePlanUpdate(d.plan_update),
  }
}

/** AgentEvent -> 与 Python Event.to_dict 同形的字典（仅含非 null 字段）。供双向 fixture 验证。 */
export function eventToDict(ev: AgentEvent): Record<string, unknown> {
  const d: Record<string, unknown> = { seq: ev.seq, type: ev.type, ts: ev.ts }
  if (ev.transient !== undefined) d.transient = ev.transient
  if (ev.decision !== undefined) d.decision = ev.decision
  if (ev.tool_use !== undefined) d.tool_use = ev.tool_use
  if (ev.tool_result !== undefined) d.tool_result = ev.tool_result
  if (ev.tool_call_id !== undefined) d.tool_call_id = ev.tool_call_id
  if (ev.tc_index !== undefined) d.tc_index = ev.tc_index
  if (ev.tc_name !== undefined) d.tc_name = ev.tc_name
  if (ev.tc_args !== undefined) d.tc_args = ev.tc_args
  if (ev.text !== undefined) d.text = ev.text
  if (ev.kind !== undefined) d.kind = ev.kind
  if (ev.error !== undefined) d.error = ev.error
  if (ev.questions !== undefined) d.questions = ev.questions
  if (ev.plan_path !== undefined) d.plan_path = ev.plan_path
  if (ev.plan_update !== undefined) d.plan_update = ev.plan_update
  return d
}
