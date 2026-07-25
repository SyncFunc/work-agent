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
//
// 子会话（subsession）段处理：父会话归约状态（尤其 toolById）**跨 subsession 段持续**，
// 确保 spawn_subagent 的 TOOL_USE（子 agent 之前）与 TOOL_RESULT（子 agent 之后）能正确配对，
// 结果回填到发起调用的那一块，而不是另起一个孤立「运行中」块。

import { useMemo } from 'react'
import type { AgentEvent, Question, ToolResult } from '../../protocol/types'

export interface TextBlock {
  key: string
  type: 'text'
  role: 'assistant'
  content: string
  reasoning: string
  final: boolean
  /** 是否仍在流式生成（仅归约后「最后一个未冲刷的」文本气泡为 true；已被工具/决策/终态
   * 冲刷出的中间思考段一律为 false，从而不再带光标、默认折叠）。 */
  streaming: boolean
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
  /** 用户的澄清回答（紧跟 clarify 的 user 事件回填，与澄清块一起渲染）。 */
  answer?: string
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

/** 由子 agent 内部块推导其运行状态（主聊天区卡片用，无需后台面板传 status）。 */
export function deriveSubagentStatus(blocks: ChatBlock[]): 'running' | 'done' {
  // 任一文本块仍在流式 → 运行中
  if (blocks.some((b) => b.type === 'text' && b.streaming)) return 'running'
  // 任一文本块已终态（final）→ 已完成
  if (blocks.some((b) => b.type === 'text' && b.final)) return 'done'
  const last = blocks[blocks.length - 1]
  if (!last) return 'running'
  if (last.type === 'error') return 'done'
  if (last.type === 'tool') return last.running ? 'running' : 'done'
  return 'running'
}

function newTextBlock(): TextBlock {
  return { key: '', type: 'text', role: 'assistant', content: '', reasoning: '', final: false, streaming: false }
}

/** 控制类工具：被循环拦截在 _exec_tools 之前（ask_clarification / present_plan），
 * 只会收到模型流式 `tool_call_delta`、永远不会收到 `tool_use`/`tool_result`，
 * 因此其 ToolBlock 必须被显式丢弃，否则会永久停留在「运行中」且暴露原始 deltaArgs。 */
const INTERCEPTED_CONTROL_TOOLS = new Set(['ask_clarification', 'present_plan'])

/** 把一段「同一 subsession」的事件归约为可渲染的块列表（纯函数、独立状态）。
 *
 * 用于子 agent 段（subsession_id 非空）。父会话段用 ``buildChatModel`` 内的共享状态归约，
 * 以保证 tool_use/tool_result 跨子 agent 段仍能配对。
 */
function reduceSubEvents(events: AgentEvent[], prefix: string): ChatBlock[] {
  const blocks: ChatBlock[] = []
  let n = 0
  let cur: TextBlock | null = null
  const toolOrder: ToolBlock[] = []
  const toolById = new Map<string, ToolBlock>()
  let toolUseSeen = 0
  let hasStreamedText = false
  let lastDecisionText: string | null = null

  const flushText = (): void => {
    if (cur && (cur.content || cur.reasoning)) {
      cur.key = `${prefix}t${n++}`
      blocks.push(cur)
      cur = null
    } else if (!hasStreamedText && lastDecisionText) {
      cur = newTextBlock()
      cur.content = lastDecisionText
      cur.key = `${prefix}t${n++}`
      blocks.push(cur)
      cur = null
    }
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
        lastDecisionText = null
        if (!cur) cur = newTextBlock()
        const text = ev.text ?? ''
        if (ev.kind === 'reasoning') cur.reasoning += text
        else cur.content += text
        break
      }
      case 'decision': {
        if (ev.decision?.text) lastDecisionText = ev.decision.text
        flushText()
        break
      }
      case 'final': {
        const ft = ev.text ?? ''
        if (cur) {
          cur.final = true
          flushText()
        } else {
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
        toolOrder.length = 0
        toolById.clear()
        toolUseSeen = 0
        hasStreamedText = false
        lastDecisionText = null
        blocks.push({ key: `${prefix}b${n++}`, type: 'user', text: ev.text ?? '' })
        break
      }
      case 'tool_call_delta': {
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
  // 段末：若仍有未冲刷的当前文本气泡（最后事件为 text 且无 final/决策/工具收尾），
  // 说明该子 agent 仍在流式生成 → 标记 streaming 供光标渲染；已冲刷的中间块保持 false。
  // 此处即完成冲刷，不要再于此前单独 flushText()，否则 streaming 标记会丢失。
  if (cur) {
    cur.streaming = true
    flushText()
  }
  return blocks
}

/** 把事件序列归约为可渲染的视图模型（纯函数）。

父会话事件做**单次顺序归约**（状态跨 subsession 段持续），子会话事件包裹成独立的
``SubagentBlock``（主聊天区独立卡片）。这样工具调用拉起的子 agent 以独立块呈现，
且 spawn_subagent 的 TOOL_USE/TOOL_RESULT 能跨子 agent 段配对，结果回填到发起块。
*/
export function buildChatModel(events: AgentEvent[]): ChatModel {
  const top: ChatBlock[] = []
  // 父会话归约状态（跨 subsession 段持续，仅定位类状态在段边界重置，toolById 始终保留）。
  let n = 0
  let cur: TextBlock | null = null as TextBlock | null
  const toolOrder: ToolBlock[] = []
  const toolById = new Map<string, ToolBlock>()
  let toolUseSeen = 0
  let hasStreamedText = false
  let lastDecisionText: string | null = null
  let subSeq = 0

  const flushText = (): void => {
    if (cur && (cur.content || cur.reasoning)) {
      cur.key = `${n++}`
      top.push(cur)
      cur = null
    } else if (!hasStreamedText && lastDecisionText) {
      cur = newTextBlock()
      cur.content = lastDecisionText
      cur.key = `${n++}`
      top.push(cur)
      cur = null
    }
    lastDecisionText = null
  }

  const ensureToolAt = (index: number): ToolBlock => {
    let tb = toolOrder[index]
    if (!tb) {
      tb = {
        key: `${n++}`,
        type: 'tool',
        toolCallId: null,
        name: 'tool',
        args: null,
        deltaArgs: '',
        result: null,
        running: true,
      }
      toolOrder[index] = tb
      top.push(tb)
    }
    return tb
  }

  /** 丢弃因被循环拦截而「悬空」的控制工具块（running 且从未收到 tool_use/tool_result）。 */
  const dropInterceptedControl = (name: string): void => {
    for (let k = top.length - 1; k >= 0; k--) {
      const b = top[k]
      if (
        b.type === 'tool' &&
        b.running &&
        b.result === null &&
        b.toolCallId === null &&
        b.name === name
      ) {
        top.splice(k, 1)
      }
    }
  }

  const processParent = (ev: AgentEvent): void => {
    switch (ev.type) {
      case 'text': {
        hasStreamedText = true
        lastDecisionText = null
        if (!cur) cur = newTextBlock()
        const text = ev.text ?? ''
        if (ev.kind === 'reasoning') cur.reasoning += text
        else cur.content += text
        break
      }
      case 'decision': {
        if (ev.decision?.text) lastDecisionText = ev.decision.text
        flushText()
        break
      }
      case 'final': {
        const ft = ev.text ?? ''
        if (cur) {
          cur.final = true
          flushText()
        } else {
          const lastText = [...top].reverse().find((b) => b.type === 'text')
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
        top.push({ key: `${n++}`, type: 'error', text: ev.error ?? '' })
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
        // 澄清回答：紧跟 clarify 的 user 事件直接回填到澄清块，与澄清一起渲染。
        const lastBlk = top[top.length - 1]
        if (lastBlk && lastBlk.type === 'clarify' && lastBlk.answer === undefined) {
          lastBlk.answer = ev.text ?? ''
        } else {
          top.push({ key: `${n++}`, type: 'user', text: ev.text ?? '' })
        }
        break
      }
      case 'tool_call_delta': {
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
          // 配对成功：结果回填到发起调用的那一块（如 spawn_subagent），并标记完成。
          tb.result = ev.tool_result ?? null
          tb.running = false
        } else {
          top.push({
            key: `${n++}`,
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
        // 丢弃因被澄清闸门拦截而悬空的 ask_clarification 工具块（否则永久「运行中」）。
        dropInterceptedControl('ask_clarification')
        top.push({ key: `${n++}`, type: 'clarify', questions: ev.questions ?? [] })
        break
      }
      case 'plan':
      case 'plan_progress': {
        flushText()
        dropInterceptedControl('present_plan')
        top.push({
          key: `${n++}`,
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

  let i = 0
  while (i < events.length) {
    const sub = events[i].subsession_id ?? null
    if (sub == null) {
      // 父会话事件：顺序归约，状态持续。
      processParent(events[i])
      i++
    } else {
      // 子会话段：先收尾父会话挂起的文本气泡，使子 agent 前后的父文本分桶清晰。
      flushText()
      const segEvents: AgentEvent[] = []
      let j = i
      while (j < events.length && (events[j].subsession_id ?? null) === sub) {
        segEvents.push(events[j])
        j++
      }
      top.push({
        key: `sub-${sub}`,
        type: 'subagent',
        subsessionId: sub,
        name: agentFromSubId(sub),
        blocks: reduceSubEvents(segEvents, `s${subSeq++}-`),
      })
      // 段结束：仅重置「定位」状态（保留 toolById 以配对跨段结果），避免段后 delta
      // 误写到段前的工具块（如 spawn_subagent 的 TOOL_USE 在段前）。
      toolOrder.length = 0
      toolUseSeen = 0
      hasStreamedText = false
      lastDecisionText = null
      i = j
    }
  }
  // 末轮：若仍有未冲刷的当前文本气泡（最后事件为 text 且未收尾），说明父会话仍在流式生成，
  // 标记 streaming 供光标渲染；被工具/决策/终态冲刷出的中间思考段保持 false（折叠、无光标）。
  // 注意：此处即完成冲刷，不要再于此前单独 flushText()，否则 streaming 标记会丢失。
  if (cur) {
    cur.streaming = true
    flushText()
  }
  // 末轮兜底：丢弃任何仍悬空的控制工具块（如澄清轮次超出上限未发 clarify 事件等边界情形）。
  for (const name of INTERCEPTED_CONTROL_TOOLS) dropInterceptedControl(name)
  return { blocks: top }
}

/** React 包装：memo 化归约（events 引用不变则不重算）。 */
export function useChatModel(events: AgentEvent[]): ChatModel {
  return useMemo(() => buildChatModel(events), [events])
}
