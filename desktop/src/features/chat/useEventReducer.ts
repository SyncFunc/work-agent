// 把 daemon 推来的 AgentEvent[] 归约为「消息 / 工具块」视图模型（纯函数，可单测）。
// 对齐 agent/core/events.py 的 EventType 与 agent/core/loop.py 的实际事件序列：
//   - 流式 TEXT（kind=content|reasoning）是内容唯一来源；DECISION/FINAL 仅作「收尾」信号，
//     其 .text 在流式场景下与已追加的 TEXT 重复，故仅在无任何 TEXT 增量时才兜底填入（非流式兼容）。
//   - TOOL_CALL_DELTA 为瞬时事件，在 TOOL_USE 之前到达；按 tc_index 累积为参数预览。
//   - TOOL_USE 补齐真实 id / 最终参数；TOOL_RESULT 填结果区。
// replay 与实时事件走同一 reducer（daemon 侧已排除 transient 事件），天然不重复。

import { useMemo } from 'react'
import type { AgentEvent, Question, ToolResult } from '../../protocol/types'

export interface TextBlock {
  key: string
  type: 'text'
  role: 'assistant'
  content: string
  reasoning: string
  final: boolean
}

export interface ToolBlock {
  key: string
  type: 'tool'
  toolCallId: string | null
  name: string
  args: Record<string, unknown> | null
  /** 流式参数预览（TOOL_CALL_DELTA 累积的原始 JSON 片段）；TOOL_USE 后由 args 权威覆盖。 */
  deltaArgs: string
  result: ToolResult | null
  running: boolean
}

export interface UserBlock {
  key: string
  type: 'user'
  text: string
}

export interface ErrorBlock {
  key: string
  type: 'error'
  text: string
}

export interface ClarifyBlock {
  key: string
  type: 'clarify'
  questions: Question[]
}

export interface PlanBlock {
  key: string
  type: 'plan'
  planPath: string | null
  stepId?: string
  status?: string
  note?: string | null
}

export type ChatBlock =
  | TextBlock
  | ToolBlock
  | UserBlock
  | ErrorBlock
  | ClarifyBlock
  | PlanBlock

export interface ChatModel {
  blocks: ChatBlock[]
}

function newTextBlock(key: string): TextBlock {
  return { key, type: 'text', role: 'assistant', content: '', reasoning: '', final: false }
}

/** 把事件序列归约为可渲染的视图模型（纯函数）。 */
export function buildChatModel(events: AgentEvent[]): ChatModel {
  const blocks: ChatBlock[] = []
  // 当前正在累积的 assistant 文本气泡（流式结束或遇非文本事件时 flush）。
  let cur: TextBlock | null = null
  // 工具块按创建顺序（delta 与 TOOL_USE 共享同一序），用于 tc_index / 顺序匹配。
  const toolOrder: ToolBlock[] = []
  // 已定稿（TOOL_USE 携带真实 id）的工具块，按 id 快速定位结果回调。
  const toolById = new Map<string, ToolBlock>()
  let toolUseSeen = 0

  const flushText = (): void => {
    if (cur && (cur.content || cur.reasoning)) blocks.push(cur)
    cur = null
  }

  const ensureToolAt = (index: number): ToolBlock => {
    let tb = toolOrder[index]
    if (!tb) {
      tb = {
        key: `tool-${index}`,
        type: 'tool',
        toolCallId: null,
        name: 'tool',
        args: null,
        deltaArgs: '',
        result: null,
        running: true,
      }
      toolOrder[index] = tb
      blocks.push(tb)
    }
    return tb
  }

  for (const ev of events) {
    switch (ev.type) {
      case 'text': {
        if (!cur) cur = newTextBlock(`t${ev.seq}`)
        const text = ev.text ?? ''
        if (ev.kind === 'reasoning') cur.reasoning += text
        else cur.content += text
        break
      }
      case 'decision': {
        // 收尾当前流式气泡：若全程无 TEXT 增量（非流式），用 decision.text 兜底。
        if (cur) {
          if (!cur.content && !cur.reasoning && ev.decision?.text) cur.content = ev.decision.text || ''
        } else if (ev.decision?.text) {
          cur = newTextBlock(`t${ev.seq}`)
          cur.content = ev.decision.text || ''
        }
        flushText()
        break
      }
      case 'final': {
        const ft = ev.text ?? ''
        if (cur) {
          if (!cur.content && !cur.reasoning && ft) cur.content = ft
          cur.final = true
        } else if (ft) {
          cur = newTextBlock(`t${ev.seq}`)
          cur.content = ft
          cur.final = true
        }
        flushText()
        break
      }
      case 'error': {
        flushText()
        blocks.push({ key: `e${ev.seq}`, type: 'error', text: ev.error ?? '' })
        break
      }
      case 'user': {
        flushText()
        blocks.push({ key: `u${ev.seq}`, type: 'user', text: ev.text ?? '' })
        break
      }
      case 'tool_call_delta': {
        // 流式参数预览前先把已累积的文本气泡落位，保证顺序正确（文本在前、工具在后）。
        flushText()
        const idx = typeof ev.tc_index === 'number' ? ev.tc_index : toolOrder.length
        const tb = ensureToolAt(idx)
        if (ev.tc_args) tb.deltaArgs += ev.tc_args
        if (ev.tc_name) tb.name = ev.tc_name
        break
      }
      case 'tool_use': {
        flushText()
        const tc = ev.tool_use
        if (!tc) break
        const tb = ensureToolAt(toolUseSeen)
        toolUseSeen += 1
        tb.toolCallId = tc.id
        tb.name = tc.name
        tb.args = tc.arguments
        tb.running = true
        toolById.set(tc.id, tb)
        break
      }
      case 'tool_result': {
        flushText()
        const id = ev.tool_call_id ?? null
        const tb = id ? toolById.get(id) : undefined
        if (tb) {
          tb.result = ev.tool_result ?? null
          tb.running = false
        } else {
          blocks.push({
            key: `tool-${id ?? ev.seq}`,
            type: 'tool',
            toolCallId: id,
            name: 'tool',
            args: null,
            deltaArgs: '',
            result: ev.tool_result ?? null,
            running: false,
          })
        }
        break
      }
      case 'clarify': {
        flushText()
        blocks.push({ key: `c${ev.seq}`, type: 'clarify', questions: ev.questions ?? [] })
        break
      }
      case 'plan':
      case 'plan_progress': {
        flushText()
        blocks.push({
          key: `p${ev.seq}`,
          type: 'plan',
          planPath: ev.plan_path ?? null,
          stepId: ev.plan_update?.step_id,
          status: ev.plan_update?.status,
          note: ev.plan_update?.note,
        })
        break
      }
      default:
        break
    }
  }
  // 收尾残留文本气泡（事件流在文本中途结束的情况）。
  flushText()
  return { blocks }
}

/** React 包装：memo 化归约（events 引用不变则不重算）。 */
export function useChatModel(events: AgentEvent[]): ChatModel {
  return useMemo(() => buildChatModel(events), [events])
}
