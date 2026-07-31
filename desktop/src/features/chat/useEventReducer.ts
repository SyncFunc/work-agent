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
import { extractPartialPath } from '../../utils/diff'
import type {
  AgentEvent,
  PlanStepView,
  PlanUpdate,
  Question,
  ToolResult,
} from '../../protocol/types'

/** 合并计划步骤：优先用后端回传的完整投影，缺失时基于增量本地 merge。 */
function mergePlanSteps(
  prev: PlanStepView[] | undefined,
  full: PlanStepView[] | null | undefined,
  update: PlanUpdate | null | undefined,
): PlanStepView[] {
  if (full) return full.map((s) => ({ ...s }))
  const arr: PlanStepView[] = prev ? prev.map((s) => ({ ...s })) : []
  if (update) {
    const si = arr.findIndex((s) => s.id === update.step_id)
    if (si >= 0) arr[si] = { ...arr[si], status: update.status }
    else arr.push({ id: update.step_id, title: update.step_id, status: update.status })
  }
  return arr
}

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
  /** write/edit 目标文件的原始内容（FILE_ORIGINAL 预读），供 DiffBlock 实时 diff。 */
  original: string | null
  /** update_plan：后端回传的完整计划列表，供前端渲染完整步骤。 */
  planSteps?: PlanStepView[]
  /** update_plan：本次更新定位的步骤（高亮用）。 */
  planUpdate?: { stepId: string; status: string; note?: string | null }
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
  /** 计划正文（PLAN 事件时由 ev.text 传入），供步骤列表渲染使用。 */
  body?: string
  /** 完整步骤列表（PLAN / PLAN_PROGRESS 事件由后端 plan_steps 投影传入）。 */
  steps?: PlanStepView[]
}

// M10.4：每轮 Token 消耗明细（与 UsageSummary 合计后挂到 ResponseBlock.turnMeta）。
export interface UsageSummary {
  prompt_tokens: number
  completion_tokens: number
  reasoning_tokens: number
  cache_hit_tokens: number
  cache_miss_tokens: number
  cache_write_tokens: number
  total_tokens: number
}
export interface TurnMeta {
  duration: number
  usage: UsageSummary
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

/** 整轮模型响应分组：把同一轮模型响应里的 text/tool/subagent/error/clarify/plan
 * 归并到一个组，主聊天区统一共享一个助手头像。这样「子 agent 之后的父层总结 text」
 * 与 subagent 卡自然同属一轮、共享头像，而 subagent 卡本身保持完整不被拆开。 */
export interface ResponseBlock {
  key: string
  type: 'response'
  role: 'assistant'
  blocks: ChatBlock[]
  /** M10.4：本响应组的 Token 消耗与耗时（由 USAGE 事件归集，子 agent 消耗已向上累加）。 */
  turnMeta?: TurnMeta | null
}

export type ChatBlock =
  | TextBlock
  | ToolBlock
  | UserBlock
  | ErrorBlock
  | ClarifyBlock
  | PlanBlock
  | SubagentBlock
  | ResponseBlock

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

/** 控制类工具中被循环拦截但前端仍需保留其块以展示「生成中」反馈的工具。
 * - `present_plan`：计划生成期间保留呼吸动画块，与 PLAN 事件块共存。
 * - `ask_clarification`/`update_plan`：无有用渲染，必须在对应事件到达时丢弃，
 *   否则永久停留在「运行中」且暴露原始 deltaArgs。 */
const INTERCEPTED_CONTROL_TOOLS = new Set(['ask_clarification', 'update_plan'])

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
  // FILE_ORIGINAL 预读缓存：path → 原文件内容（write/edit 实时 diff 用）
  const originalByPath = new Map<string, string>()

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
        original: null,
      }
      toolOrder[index] = tb
      blocks.push(tb)
    }
    return tb
  }

  // write/edit：从 args.path（优先）或 deltaArgs 提取目标路径，命中预读缓存则挂载原内容。
  const attachOriginal = (tb: ToolBlock): void => {
    if (tb.original !== null) return
    if (tb.name !== 'write' && tb.name !== 'edit') return
    const argPath = typeof tb.args?.path === 'string' ? tb.args.path : null
    const path = argPath ?? extractPartialPath(tb.deltaArgs)
    if (path) {
      const o = originalByPath.get(path)
      if (o !== undefined) tb.original = o
    }
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
      case 'file_original': {
        // write/edit 流式预读：缓存 path → 原内容，并直接挂载到已匹配的工具块
        // （路径来自后端解析，比前端 deltaArgs 正则提取更可靠，保证写入中实时 diff 可用）。
        if (ev.file_path && ev.file_original != null) {
          originalByPath.set(ev.file_path, ev.file_original)
          for (const tb of toolOrder) {
            if (
              tb.original === null &&
              (tb.name === 'write' || tb.name === 'edit') &&
              (typeof tb.args?.path === 'string' ? tb.args.path : extractPartialPath(tb.deltaArgs)) === ev.file_path
            ) {
              tb.original = ev.file_original
            }
          }
        }
        break
      }
      case 'tool_call_delta': {
        flushText()
        const idx = typeof ev.tc_index === 'number' ? ev.tc_index : toolOrder.length
        // 跨轮检测（同父会话逻辑）：已有块 → 两种「新轮」信号
        const exD = toolOrder[idx]
        if (
          exD &&
          (
            // ① 旧块已收尾（有 result）→ 新轮开始
            (!exD.running && exD.result !== null) ||
            // ② 旧块名称与新工具不同（如 present_plan 永远 running，新轮 update_plan 同名撞 slot）
            (ev.tc_name && exD.name !== ev.tc_name)
          )
        ) {
          toolOrder.length = 0
          toolById.clear()
          toolUseSeen = 0
        }
        const tb = ensureToolAt(idx)
        if (ev.tc_args) tb.deltaArgs += ev.tc_args
        if (ev.tc_name) tb.name = ev.tc_name
        attachOriginal(tb)
        break
      }
      case 'tool_use': {
        flushText()
        const tc = ev.tool_use
        if (!tc) break
        // 跨轮检测（同父会话逻辑）
        const exU = toolOrder[toolUseSeen]
        if (
          exU &&
          (
            (!exU.running && exU.result !== null) ||
            (tc.name && exU.name !== tc.name)
          )
        ) {
          toolOrder.length = 0
          toolById.clear()
          toolUseSeen = 0
        }
        const tb = ensureToolAt(toolUseSeen)
        toolUseSeen += 1
        tb.toolCallId = tc.id
        tb.name = tc.name
        tb.args = tc.arguments
        tb.running = true
        toolById.set(tc.id, tb)
        attachOriginal(tb)
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
          const orphan = {
            key: `${prefix}tool-${n++}`,
            type: 'tool' as const,
            toolCallId: id,
            name: 'tool',
            args: null,
            deltaArgs: '',
            result: ev.tool_result ?? null,
            running: false,
            original: null,
          }
          blocks.push(orphan)
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
        // 合并连续的 plan/progress：找到最后一个 plan 块，更新其属性
        const lastPlan = blocks.findLast((b) => b.type === 'plan') as PlanBlock | undefined
        if (lastPlan) {
          if (ev.plan_path != null) lastPlan.planPath = ev.plan_path
          if (ev.plan_update?.step_id != null) lastPlan.stepId = ev.plan_update.step_id
          if (ev.plan_update?.status != null) lastPlan.status = ev.plan_update.status
          if (ev.plan_update?.note != null) lastPlan.note = ev.plan_update.note
          if (ev.text != null) lastPlan.body = ev.text
          lastPlan.steps = mergePlanSteps(lastPlan.steps, ev.plan_steps, ev.plan_update)
        } else {
          blocks.push({
            key: `${prefix}b${n++}`,
            type: 'plan',
            planPath: ev.plan_path ?? null,
            stepId: ev.plan_update?.step_id,
            status: ev.plan_update?.status,
            note: ev.plan_update?.note,
            body: ev.text ?? undefined,
            steps: mergePlanSteps(undefined, ev.plan_steps, ev.plan_update),
          })
        }
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
  // M10.4 debug: 打印收到的 USAGE 事件（用于排查前端用量不显示问题；提交前可保留）。
  const usageEvents = events.filter((e) => e.type === 'usage')
  if (usageEvents.length > 0) {
    console.log(
      '[M10.4-debug] USAGE events:',
      usageEvents.map((e) => ({
        message_id: e.message_id,
        parent_message_id: e.parent_message_id,
        usage: e.usage,
        duration: e.duration,
        estimated: e.estimated,
      })),
    )
  }
  let top: ChatBlock[] = []
  // 父会话归约状态（跨 subsession 段持续，仅定位类状态在段边界重置，toolById 始终保留）。
  let n = 0
  let cur: TextBlock | null = null as TextBlock | null
  const toolOrder: ToolBlock[] = []
  const toolById = new Map<string, ToolBlock>()
  let toolUseSeen = 0
  let hasStreamedText = false
  let lastDecisionText: string | null = null
  let subSeq = 0
  // FILE_ORIGINAL 预读缓存：path → 原文件内容（write/edit 实时 diff 用）。
  const originalByPath = new Map<string, string>()

  // M10.4：用量归集——子 agent(subession) 与父会话 USAGE 事件均累加到 root ResponseBlock 的 turnMeta。
  // "队列"机制：每个 USER 事件标记新一轮，flush 上一轮用量。
  const usageQueue: TurnMeta[] = []
  let currentUsage: TurnMeta | null = null
  let usageDirty = false

  const flushText = (): void => {
    if (cur && (cur.content || cur.reasoning)) {
      cur.key = `${n++}`
      top.push(cur)
      cur = null
    } else if (!hasStreamedText && lastDecisionText) {
      // 不立即 push：先保留在 cur，等待紧随其后的 text/final 归并到同一气泡，
      // 避免「decision 兜底块」与后续 text 各自成块（子 agent 前后多出助手头像）。
      cur = newTextBlock()
      cur.content = lastDecisionText
      cur.key = `${n++}`
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
        original: null,
      }
      toolOrder[index] = tb
      top.push(tb)
    }
    return tb
  }

  // write/edit：从 args.path（优先）或 deltaArgs 提取目标路径，命中预读缓存则挂载原内容。
  const attachOriginal = (tb: ToolBlock): void => {
    if (tb.original !== null) return
    if (tb.name !== 'write' && tb.name !== 'edit') return
    const argPath = typeof tb.args?.path === 'string' ? tb.args.path : null
    const path = argPath ?? extractPartialPath(tb.deltaArgs)
    if (path) {
      const o = originalByPath.get(path)
      if (o !== undefined) tb.original = o
    }
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
        // M10.4：USER 事件标记新一轮：flush 上一轮的用量到队列。
        if (usageDirty && currentUsage) {
          usageQueue.push(currentUsage)
          currentUsage = null
          usageDirty = false
        }
        break
      }
      case 'file_original': {
        // write/edit 流式预读：缓存 path → 原内容，并直接挂载到已匹配的工具块
        // （路径来自后端解析，比前端 deltaArgs 正则提取更可靠，保证写入中实时 diff 可用）。
        if (ev.file_path && ev.file_original != null) {
          originalByPath.set(ev.file_path, ev.file_original)
          for (const tb of toolOrder) {
            if (
              tb.original === null &&
              (tb.name === 'write' || tb.name === 'edit') &&
              (typeof tb.args?.path === 'string' ? tb.args.path : extractPartialPath(tb.deltaArgs)) === ev.file_path
            ) {
              tb.original = ev.file_original
            }
          }
        }
        break
      }
      case 'tool_call_delta': {
        flushText()
        const idx = typeof ev.tc_index === 'number' ? ev.tc_index : toolOrder.length
        // 跨轮检测：如果该 slot 已有块且满足以下任一条件→新轮开始，擦除旧定位状态：
        //   ① 旧块已收尾（有 result、非 running）
        //   ② 旧块名称与新工具不同（present_plan 永远 running，新轮同名撞 slot 会误写）
        const exD = toolOrder[idx]
        if (
          exD &&
          (
            (!exD.running && exD.result !== null) ||
            (ev.tc_name && exD.name !== ev.tc_name)
          )
        ) {
          toolOrder.length = 0
          toolById.clear()
          toolUseSeen = 0
        }
        const tb = ensureToolAt(idx)
        if (ev.tc_args) tb.deltaArgs += ev.tc_args
        if (ev.tc_name) tb.name = ev.tc_name
        attachOriginal(tb)
        break
      }
      case 'tool_use': {
        flushText()
        const tc = ev.tool_use
        if (!tc) break
        // 跨轮检测（处理回放场景，回放无 tool_call_delta）
        const exU = toolOrder[toolUseSeen]
        if (
          exU &&
          (
            (!exU.running && exU.result !== null) ||
            (tc.name && exU.name !== tc.name)
          )
        ) {
          toolOrder.length = 0
          toolById.clear()
          toolUseSeen = 0
        }
        const tb = ensureToolAt(toolUseSeen)
        toolUseSeen += 1
        tb.toolCallId = tc.id
        tb.name = tc.name
        tb.args = tc.arguments
        tb.running = true
        toolById.set(tc.id, tb)
        attachOriginal(tb)
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
            original: null,
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
        dropInterceptedControl('update_plan')
        // 合并连续的 plan/progress：找到最后一个 plan 块，更新其属性
        const lastPlan = top.findLast((b) => b.type === 'plan') as PlanBlock | undefined
        if (lastPlan) {
          if (ev.plan_path != null) lastPlan.planPath = ev.plan_path
          if (ev.plan_update?.step_id != null) lastPlan.stepId = ev.plan_update.step_id
          if (ev.plan_update?.status != null) lastPlan.status = ev.plan_update.status
          if (ev.plan_update?.note != null) lastPlan.note = ev.plan_update.note
          if (ev.text != null) lastPlan.body = ev.text
          lastPlan.steps = mergePlanSteps(lastPlan.steps, ev.plan_steps, ev.plan_update)
        } else {
          top.push({
            key: `${n++}`,
            type: 'plan',
            planPath: ev.plan_path ?? null,
            stepId: ev.plan_update?.step_id,
            status: ev.plan_update?.status,
            note: ev.plan_update?.note,
            body: ev.text ?? undefined,
            steps: mergePlanSteps(undefined, ev.plan_steps, ev.plan_update),
          })
        }
        // 把完整计划列表 + 本次更新定位回填到「进行中、尚未收到结果」的 update_plan 工具块，
        // 由 ToolBlock 渲染完整步骤列表（而非丑陋的 JSON 参数块）。
        if (ev.plan_steps || ev.plan_update) {
          for (let k = top.length - 1; k >= 0; k--) {
            const b = top[k]
            if (
              b.type === 'tool' &&
              b.name === 'update_plan' &&
              b.running &&
              b.result === null
            ) {
              const tb = b as ToolBlock
              tb.planSteps = ev.plan_steps
                ? ev.plan_steps.map((s) => ({ ...s }))
                : tb.planSteps
              if (ev.plan_update) {
                tb.planUpdate = {
                  stepId: ev.plan_update.step_id,
                  status: ev.plan_update.status,
                  note: ev.plan_update.note,
                }
              }
              break
            }
          }
        }
        break
      }
      case 'usage': {
        const u = ev.usage ?? {}
        if (!currentUsage) {
          currentUsage = {
            duration: 0,
            usage: {
              prompt_tokens: 0,
              completion_tokens: 0,
              reasoning_tokens: 0,
              cache_hit_tokens: 0,
              cache_miss_tokens: 0,
              cache_write_tokens: 0,
              total_tokens: 0,
            },
          }
        }
        currentUsage.duration += ev.duration ?? 0
        currentUsage.usage.prompt_tokens += u.prompt_tokens ?? 0
        currentUsage.usage.completion_tokens += u.completion_tokens ?? 0
        currentUsage.usage.reasoning_tokens += u.reasoning_tokens ?? 0
        currentUsage.usage.cache_hit_tokens += u.cache_hit_tokens ?? 0
        currentUsage.usage.cache_miss_tokens += u.cache_miss_tokens ?? 0
        currentUsage.usage.cache_write_tokens += u.cache_write_tokens ?? 0
        currentUsage.usage.total_tokens += u.total_tokens ?? 0
        usageDirty = true
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
      // M10.4：提取子 agent 段内的 USAGE 事件，累积到父会话的用量（子 agent 块不渲染消耗）。
      for (const sev of segEvents) {
        if (sev.type !== 'usage') continue
        if (!currentUsage) {
          currentUsage = {
            duration: 0,
            usage: {
              prompt_tokens: 0,
              completion_tokens: 0,
              reasoning_tokens: 0,
              cache_hit_tokens: 0,
              cache_miss_tokens: 0,
              cache_write_tokens: 0,
              total_tokens: 0,
            },
          }
        }
        const u = sev.usage ?? {}
        currentUsage.duration += sev.duration ?? 0
        currentUsage.usage.prompt_tokens += u.prompt_tokens ?? 0
        currentUsage.usage.completion_tokens += u.completion_tokens ?? 0
        currentUsage.usage.reasoning_tokens += u.reasoning_tokens ?? 0
        currentUsage.usage.cache_hit_tokens += u.cache_hit_tokens ?? 0
        currentUsage.usage.cache_miss_tokens += u.cache_miss_tokens ?? 0
        currentUsage.usage.cache_write_tokens += u.cache_write_tokens ?? 0
        currentUsage.usage.total_tokens += u.total_tokens ?? 0
        usageDirty = true
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
    // 仅当本轮确有流式文本时才显示光标；纯 decision 兜底块（无真实文本）视为已收尾。
    cur.streaming = hasStreamedText
    flushText()
  }
  // 末轮兜底：丢弃任何仍悬空的控制工具块（如澄清轮次超出上限未发 clarify 事件等边界情形）。
  for (const name of INTERCEPTED_CONTROL_TOOLS) dropInterceptedControl(name)

  // M10.4：flush 最后累积的用量。
  if (usageDirty && currentUsage) {
    usageQueue.push(currentUsage)
    currentUsage = null
    usageDirty = false
  }

  // 整轮响应分组（Bug4 修复核心机制）：把顶层「连续的非 user 块」归并为一个
  // ResponseBlock，主聊天区统一共享一个助手头像。user 块作为独立分隔符不进组
  // （每个用户输入开启新一轮）。这样子 agent 段之后的父层总结 text 与 subagent 卡
  // 自然同属一轮、共享头像，而 subagent 卡本身保持完整不被拆开；澄清答案回填
  // clarify 块、其 user 已在归约期被消费而不产生新顶层 user，故澄清块连同答案同属一轮。
  const grouped: ChatBlock[] = []
  let curGroup: ChatBlock[] = []
  let gi = 0
  const flushGroup = (): void => {
    if (curGroup.length > 0) {
      grouped.push({ key: `r${gi++}`, type: 'response', role: 'assistant', blocks: curGroup })
      curGroup = []
    }
  }
  for (const b of top) {
    if (b.type === 'user') {
      flushGroup()
      grouped.push(b)
    } else {
      curGroup.push(b)
    }
  }
  flushGroup()
  top = grouped

  // M10.4：按顺序把用量挂到 ResponseBlock 的 turnMeta。
  {
    let idx = 0
    for (const g of top) {
      if (g.type === 'response') {
        if (idx < usageQueue.length) {
          (g as ResponseBlock).turnMeta = usageQueue[idx]
        }
        idx++
      }
    }
  }

  // M10.4 debug: 打印 ResponseBlock 与 turnMeta（用于排查前端用量不显示；提交前可保留）。
  {
    const metas = top
      .filter((g): g is ResponseBlock => g.type === 'response')
      .map((r) => ({ key: r.key, turnMeta: r.turnMeta }))
    if (metas.some((m) => m.turnMeta !== undefined)) {
      console.log('[M10.4-debug] ResponseBlock turnMeta:', JSON.stringify(metas))
    }
  }

  // —— 调试钩子（仅用户主动开启；不影响生产/测试）——
  // 开启方式：Electron DevTools 控制台执行 localStorage.setItem('chat-debug','1') 后重新触发对话，
  // 过滤日志前缀 [chat-debug] 即可看到「原始事件流(含 subsession_id)」与「归约后的块结构」。
  // 用于定位「子 agent 之后多出助手头像」等问题：重点观察 blocks 里 subagent 之后的 text 是否已与
  // subagent 同属一个 response 组（responses=N 表示主聊天区助手头像气泡数）。
  if (typeof window !== 'undefined' && window.localStorage?.getItem('chat-debug') === '1') {
    const evSummary = events.map((e) => {
      const o: Record<string, unknown> = {
        seq: e.seq,
        type: e.type,
        sub: (e as { subsession_id?: string | null }).subsession_id ?? null,
      }
      if (e.type === 'text' && typeof e.text === 'string') o.t = e.text.slice(0, 40)
      if (e.type === 'tool_result')
        o.res = String((e as { tool_result?: unknown }).tool_result ?? '').slice(0, 56)
      return o
    })
    const sumBlocks = (bs: ChatBlock[]): unknown[] =>
      bs.map((b) => {
        if (b.type === 'text')
          return {
            type: 'text',
            content: (b.content || '').slice(0, 40),
            reasoning: (b.reasoning || '').slice(0, 12),
            final: b.final,
          }
        if (b.type === 'tool')
          return { type: 'tool', name: b.name, res: String(b.result ?? '').slice(0, 56) }
        if (b.type === 'subagent')
          return {
            type: 'subagent',
            sub: (b as SubagentBlock).subsessionId,
            inner: (b as SubagentBlock).blocks.length,
          }
        if (b.type === 'response') return { type: 'response', blocks: sumBlocks((b as ResponseBlock).blocks) }
        return { type: b.type }
      })
    const blSummary = sumBlocks(top)
    const topResponses = top.filter((b) => b.type === 'response').length
    const topTexts = top.filter((b) => b.type === 'text').length
    const topUsers = top.filter((b) => b.type === 'user').length
    console.log('[chat-debug] events', evSummary)
    console.log(
      `[chat-debug] blocks (responses=${topResponses}, topTexts=${topTexts}, users=${topUsers})`,
      blSummary,
    )
    const walk = (bs: ChatBlock[]): void => {
      for (const b of bs) {
        if (b.type === 'subagent') {
          const sb = b as SubagentBlock
          const inner = sb.blocks.map((ib) => {
            if (ib.type === 'text')
              return { type: 'text', content: (ib.content || '').slice(0, 44), final: ib.final }
            return { type: ib.type }
          })
          console.log(`[chat-debug] subagent(${sb.subsessionId}) inner(${sb.blocks.length})`, inner)
        } else if (b.type === 'response') {
          walk((b as ResponseBlock).blocks)
        }
      }
    }
    walk(top)
  }

  return { blocks: top }
}

/** React 包装：memo 化归约（events 引用不变则不重算）。 */
let _chatDebugHinted = false
export function useChatModel(events: AgentEvent[]): ChatModel {
  if (typeof window !== 'undefined' && !_chatDebugHinted) {
    _chatDebugHinted = true
    const on = window.localStorage?.getItem('chat-debug') === '1'
    console.log(
      `[chat-debug] 已加载。当前${on ? '已开启' : '未开启'}：在 DevTools 控制台执行 ` +
        `localStorage.setItem('chat-debug','1') 后重新触发一次对话，` +
        `过滤日志前缀 '[chat-debug]' 即可查看原始事件流与归约块结构。`,
    )
  }
  return useMemo(() => buildChatModel(events), [events])
}
