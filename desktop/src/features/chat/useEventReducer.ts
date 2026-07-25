// 把 daemon 推来的 AgentEvent[] 归约为「消息 / 工具块」视图模型（纯函数，可单测）。
// 对齐 agent/core/events.py 的 EventType 与 agent/core/loop.py 的实际事件序列：
//   - 流式 TEXT（kind=content|reasoning）是内容唯一来源；DECISION/FINAL 仅作「收尾」信号，
//     其 .text 在流式场景下与已追加的 TEXT 重复，故仅在无任何 TEXT 增量时才兜底填入（非流式兼容）。
//   - TOOL_CALL_DELTA 为瞬时事件，在 TOOL_USE 之前到达；按 tc_index 累积为参数预览。
//   - TOOL_USE 补齐真实 id / 最终参数；TOOL_RESULT 填结果区。
// replay 与实时事件走同一 reducer（daemon 侧已排除 transient 事件），天然不重复。
//
// 关键不变量（修复「重复渲染 / 上一轮消息在本轮重复 / 越来越多」）：
//   daemon 每轮 step 都会新建 EventStream，事件 seq 从 0 重新递增，且 event_buffer 跨轮累积。
//   因此**绝不能用 ev.seq / tc_index 做 React 的块 key 或跨块定位依据**——否则跨轮 seq 碰撞会让
//   React 复用错误 DOM、工具块跨轮错乱。这里一律用遍历时的全局递增计数器 n 生成唯一 key，
//   并在每轮 USER 事件（标记新一轮）重置工具定位状态。

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

/** 子 agent（subsession）独立块：在主聊天区以卡片呈现，内部是它自己的事件归约视图。 */
export interface SubagentBlock {
  key: string
  type: 'subagent'
  /** 子会话 id（subsession_id），用于前端分桶与历史重建。 */
  subsessionId: string
  /** 从 subsession_id 解析出的 agent 名（用于顶栏 `subagent:<name>`）。 */
  name: string
  blocks: ChatBlock[]
}

export type ChatBlock =
  | TextBlock
  | ToolBlock
  | UserBlock
  | ErrorBlock
  | ClarifyBlock
  | PlanBlock
  | SubagentBlock

export interface ChatModel {
  blocks: ChatBlock[]
}

/** 从 subsession_id（格式 `<parent>/sub_<agent>_<depth>_<uuid>`）解析 agent 名。 */
export function agentFromSubId(subId: string): string {
  const m = subId.match(/sub_(.+)_(\d+)_[0-9a-f]+$/)
  return m ? m[1] : subId
}

function newTextBlock(): TextBlock {
  return { key: '', type: 'text', role: 'assistant', content: '', reasoning: '', final: false }
}

/** 把一段「同一来源（父会话 或 同一 subsession）」的事件归约为可渲染的块列表（纯函数）。

``prefix`` 用于保证跨分段块 key 全局唯一（不同分段各用不同前缀，避免 React key 碰撞）。
详见 ``buildChatModel`` 的分段逻辑。
*/
function reduceEvents(events: AgentEvent[], prefix: string): ChatBlock[] {
  const blocks: ChatBlock[] = []
  // 全局块序号：每个块分配唯一 key，杜绝跨轮 seq 碰撞导致的 React 复用错乱。
  let n = 0
  // 当前正在累积的 assistant 文本气泡（流式结束或遇非文本事件时 flush）。
  let cur: TextBlock | null = null
  // 工具块按创建顺序（delta 与 TOOL_USE 共享同一序），用于 tc_index / 顺序匹配。
  const toolOrder: ToolBlock[] = []
  // 已定稿（TOOL_USE 携带真实 id）的工具块，按 id 快速定位结果回调。
  const toolById = new Map<string, ToolBlock>()
  let toolUseSeen = 0
  // 本轮是否出现过流式 TEXT（内容唯一来源）。
  // 用于约束 decision.text 仅在本轮「完全无流式 TEXT」时才兜底填入，避免与流式 TEXT 累积成两个
  // 相同内容的气泡（重复渲染）。
  let hasStreamedText = false
  // decision.text 仅作兜底：仅当本轮无流式 TEXT 时消费一次，且消费后立即清空，
  // 防止跨轮残留造成「上一轮文本在本轮重复 / 越来越多」。
  let lastDecisionText: string | null = null

  const flushText = (): void => {
    // 1) 优先落已累积的流式气泡（内容唯一来源）。
    if (cur && (cur.content || cur.reasoning)) {
      cur.key = `${prefix}t${n++}`
      blocks.push(cur)
      cur = null
    } else if (!hasStreamedText && lastDecisionText) {
      // 2) 仅当本轮完全没有流式 TEXT 时，才用 decision.text 兜底补一个气泡（非流式兼容）。
      //    消费后立即清空，杜绝跨轮残留重复。
      cur = newTextBlock()
      cur.content = lastDecisionText
      cur.key = `${prefix}t${n++}`
      blocks.push(cur)
      cur = null
    }
    // 无论走哪条分支都清空兜底文本，避免其在后续轮次被误消费。
    lastDecisionText = null
  }

  const ensureToolAt = (index: number): ToolBlock => {
    let tb = toolOrder[index]
    if (!tb) {
      tb = {
        key: `${prefix}tool-${n++}`,
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
        hasStreamedText = true
        lastDecisionText = null // 流式文本是内容唯一来源，丢弃任何残留兜底
        if (!cur) cur = newTextBlock()
        const text = ev.text ?? ''
        if (ev.kind === 'reasoning') cur.reasoning += text
        else cur.content += text
        break
      }
      case 'decision': {
        // 流式 TEXT 才是内容唯一来源；decision.text 仅记录为兜底。
        if (ev.decision?.text) {
          lastDecisionText = ev.decision.text
        }
        // 每轮决策收尾：把已累积的流式文本气泡定稿为独立块，下一轮 text 开启新气泡
        // （否则多轮回复会挤进同一气泡，且可能与后续 FINAL 叠加造成重复渲染）。
        flushText()
        break
      }
      case 'final': {
        const ft = ev.text ?? ''
        if (cur) {
          // 仍在累积的气泡：直接收尾（流式文本已在此气泡，无需额外气泡）。
          cur.final = true
          flushText()
        } else {
          // 文本已在之前的工具/思考气泡中被 flushText 提前收成气泡（例如流式文本后跟工具调用，
          // tool_call_delta 触发 flush），此时不能再拿 final.text 另起一个相同内容的气泡，
          // 否则文本内容重复渲染、而思考（reasoning，在同一条气泡里）只出现一次。
          // 改为把最后一个已落块的文本气泡标记为 final；仅当完全没有任何文本气泡时，
          // 才用 final.text / 兜底 decision.text 补一个（非流式兼容）。
          const lastText = [...blocks].reverse().find((b) => b.type === 'text')
          if (lastText) {
            lastText.final = true
          } else if (ft || lastDecisionText) {
            cur = newTextBlock()
            cur.content = ft || lastDecisionText || ''
            cur.final = true
            flushText()
          }
        }
        break
      }
      case 'error': {
        flushText()
        blocks.push({ key: `${prefix}b${n++}`, type: 'error', text: ev.error ?? '' })
        break
      }
      case 'user': {
        flushText()
        // 每轮用户输入标记新一轮开始：重置工具定位状态，避免跨轮 tc_index / tool_use 序号
        // 复用导致工具块错乱（第二轮 delta 误写到第一轮工具块）；同时清空文本兜底残留，
        // 防止上一轮 decision.text 在本轮兜底时重复出现。
        toolOrder.length = 0
        toolById.clear()
        toolUseSeen = 0
        hasStreamedText = false
        lastDecisionText = null
        blocks.push({ key: `${prefix}b${n++}`, type: 'user', text: ev.text ?? '' })
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
            key: `${prefix}tool-${n++}`,
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
        blocks.push({ key: `${prefix}b${n++}`, type: 'clarify', questions: ev.questions ?? [] })
        break
      }
      case 'plan':
      case 'plan_progress': {
        flushText()
        blocks.push({
          key: `${prefix}b${n++}`,
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
  return blocks
}

/** 把事件序列归约为可渲染的视图模型（纯函数）。

按 ``subsession_id`` 分段：连续同源（同一 subsession 或父会话）的事件归约为一组块；
父会话事件直接展开为顶层块，子会话事件则包裹成独立的 ``SubagentBlock``（主聊天区独立卡片）。
这样工具调用拉起的子 agent、后台子 agent 都以独立块呈现，且与父会话事件保持时间顺序。
*/
export function buildChatModel(events: AgentEvent[]): ChatModel {
  const top: ChatBlock[] = []
  let seg = 0
  let i = 0
  while (i < events.length) {
    const sub = events[i].subsession_id ?? null
    const segEvents: AgentEvent[] = []
    let j = i
    while (j < events.length && (events[j].subsession_id ?? null) === sub) {
      segEvents.push(events[j])
      j++
    }
    if (sub == null) {
      top.push(...reduceEvents(segEvents, `p${seg}-`))
    } else {
      top.push({
        key: `sub-${sub}`,
        type: 'subagent',
        subsessionId: sub,
        name: agentFromSubId(sub),
        blocks: reduceEvents(segEvents, `s${seg}-`),
      })
    }
    seg++
    i = j
  }
  return { blocks: top }
}

/** React 包装：memo 化归约（events 引用不变则不重算）。 */
export function useChatModel(events: AgentEvent[]): ChatModel {
  return useMemo(() => buildChatModel(events), [events])
}
