import { describe, expect, it } from 'vitest'
import { buildChatModel, deriveSubagentStatus, type ChatBlock, type ResponseBlock, type SubagentBlock } from './useEventReducer'
import type { AgentEvent } from '../../protocol/types'

/** 解开整轮响应分组：把 ResponseBlock 展开为其内部块（子 agent 内部块不二次展开）。 */
function flatten(blocks: ChatBlock[]): ChatBlock[] {
  const out: ChatBlock[] = []
  for (const b of blocks) {
    if (b.type === 'response') out.push(...flatten(b.blocks))
    else out.push(b)
  }
  return out
}
function toolBlocks(blocks: ChatBlock[]) {
  return flatten(blocks).filter((b): b is Extract<ChatBlock, { type: 'tool' }> => b.type === 'tool')
}
function textBlocks(blocks: ChatBlock[]) {
  return flatten(blocks).filter((b): b is Extract<ChatBlock, { type: 'text' }> => b.type === 'text')
}
function subBlocks(blocks: ChatBlock[]) {
  return flatten(blocks).filter((b): b is Extract<ChatBlock, { type: 'subagent' }> => b.type === 'subagent')
}

describe('buildChatModel', () => {
  it('流式文本累积 content/reasoning，DECISION 收尾不重复', () => {
    const events: AgentEvent[] = [
      { seq: 0, type: 'text', text: 'Hello ', kind: 'content', ts: 0 },
      { seq: 1, type: 'text', text: 'world', kind: 'content', ts: 0 },
      { seq: 2, type: 'text', text: '思考', kind: 'reasoning', ts: 0 },
      { seq: 3, type: 'decision', decision: { text: 'Hello world', tool_calls: [] }, ts: 0 },
    ]
    const { blocks } = buildChatModel(events)
    const texts = textBlocks(blocks)
    expect(texts).toHaveLength(1)
    expect(texts[0].content).toBe('Hello world')
    expect(texts[0].reasoning).toBe('思考')
  })

  it('两个 assistant 轮次产生两个文本块', () => {
    const events: AgentEvent[] = [
      { seq: 0, type: 'text', text: 'a', kind: 'content', ts: 0 },
      { seq: 1, type: 'decision', decision: { text: 'a', tool_calls: [] }, ts: 0 },
      { seq: 2, type: 'text', text: 'b', kind: 'content', ts: 0 },
      { seq: 3, type: 'decision', decision: { text: 'b', tool_calls: [] }, ts: 0 },
    ]
    expect(textBlocks(buildChatModel(events).blocks)).toHaveLength(2)
  })

  it('工具流：delta 预览 + TOOL_USE 定稿 + TOOL_RESULT 结果，块唯一', () => {
    const events: AgentEvent[] = [
      { seq: 0, type: 'text', text: 'ok', kind: 'content', ts: 0 },
      // 后端发累计值：后一个 tc_args 含全部前缀（前端赋值而非追加）
      { seq: 1, type: 'tool_call_delta', tc_index: 0, tc_name: 'bash', tc_args: '{"cmd"', ts: 0 },
      { seq: 2, type: 'tool_call_delta', tc_index: 0, tc_args: '{"cmd":"ls"}', ts: 0 },
      { seq: 3, type: 'decision', decision: { text: 'ok', tool_calls: [{ id: 'c1', name: 'bash', arguments: { cmd: 'ls' } }] }, ts: 0 },
      { seq: 4, type: 'tool_use', tool_use: { id: 'c1', name: 'bash', arguments: { cmd: 'ls' } }, ts: 0 },
      { seq: 5, type: 'tool_result', tool_call_id: 'c1', tool_result: { ok: true, output: 'a\nb' }, ts: 0 },
    ]
    const { blocks } = buildChatModel(events)
    const tools = toolBlocks(blocks)
    expect(tools).toHaveLength(1)
    expect(tools[0].name).toBe('bash')
    expect(tools[0].args).toEqual({ cmd: 'ls' })
    expect(tools[0].deltaArgs).toBe('{"cmd":"ls"}')
    expect(tools[0].result).toEqual({ ok: true, output: 'a\nb' })
    expect(tools[0].running).toBe(false)
    // 文本块在工具块之前（顺序正确）；整轮响应分组后两者同属一个 response 组内
    const flat = flatten(blocks)
    expect(flat[0].type).toBe('text')
    expect(flat[1].type).toBe('tool')
  })

  it('write 实时 diff：FILE_ORIGINAL 缓存 + deltaArgs 赋值（后端累计值）命中挂载', () => {
    // 后端 emit 顺序：file_original 先到，tool_call_delta 携带「累计值」（非增量）
    const events: AgentEvent[] = [
      { seq: 0, type: 'file_original', file_path: 'src/a.ts', file_original: 'line1\nline2\n', ts: 0 },
      { seq: 1, type: 'tool_call_delta', tc_index: 0, tc_name: 'write', tc_args: '{"path":"src/a.ts","content":"line1', ts: 0 },
      // 后端每次发累计值（含全部前缀），前端应赋值而非追加
      { seq: 2, type: 'tool_call_delta', tc_index: 0, tc_name: 'write', tc_args: '{"path":"src/a.ts","content":"line1\nline2\nNEW"}' as string, ts: 0 },
      { seq: 3, type: 'tool_use', tool_use: { id: 'w1', name: 'write', arguments: { path: 'src/a.ts', content: 'line1\nline2\nNEW' } }, ts: 0 },
      { seq: 4, type: 'tool_result', tool_call_id: 'w1', tool_result: { ok: true, original: 'line1\nline2\n' }, ts: 0 },
    ]
    const { blocks } = buildChatModel(events)
    const tools = toolBlocks(blocks)
    expect(tools).toHaveLength(1)
    expect(tools[0].name).toBe('write')
    // FILE_ORIGINAL 缓存命中，attachOriginal 用完整 deltaArgs 提取 path 挂载（不创建重复块）
    expect(tools[0].original).toBe('line1\nline2\n')
    // deltaArgs 为累计值（赋值，非重复追加乱码）
    expect(tools[0].deltaArgs).toBe('{"path":"src/a.ts","content":"line1\nline2\nNEW"}')
    // 工具块最终参数 + 结果完整
    expect(tools[0].args).toEqual({ path: 'src/a.ts', content: 'line1\nline2\nNEW' })
    expect(tools[0].result).toEqual({ ok: true, original: 'line1\nline2\n' })
  })

  it('write：path 未完整时提取不到，original 不挂载（保持 null）', () => {
    // 首个 delta 的 path 只有部分，且无后续完整 delta（模拟提取失败场景）
    const events: AgentEvent[] = [
      { seq: 0, type: 'file_original', file_path: 'src/a.ts', file_original: 'line1\n', ts: 0 },
      { seq: 1, type: 'tool_call_delta', tc_index: 0, tc_name: 'write', tc_args: '{"path":"src/', ts: 0 },
    ]
    const { blocks } = buildChatModel(events)
    const tools = toolBlocks(blocks)
    expect(tools).toHaveLength(1)
    // path 不完整 → 提取不到 → original 保持 null（DiffBlock 回退到内容预览）
    expect(tools[0].original).toBe(null)
  })

  it('replay 一致性：不含 transient delta 的回放，工具最终参数/结果与带 delta 的实时一致', () => {
    const withDelta: AgentEvent[] = [
      // 后端发累计值：后一个 tc_args 含全部前缀
      { seq: 0, type: 'tool_call_delta', tc_index: 0, tc_name: 'bash', tc_args: '{"cmd"', ts: 0 },
      { seq: 1, type: 'tool_call_delta', tc_index: 0, tc_args: '{"cmd":"ls"}', ts: 0 },
      { seq: 2, type: 'decision', decision: { text: '', tool_calls: [{ id: 'c1', name: 'bash', arguments: { cmd: 'ls' } }] }, ts: 0 },
      { seq: 3, type: 'tool_use', tool_use: { id: 'c1', name: 'bash', arguments: { cmd: 'ls' } }, ts: 0 },
      { seq: 4, type: 'tool_result', tool_call_id: 'c1', tool_result: { ok: true, output: 'OUT' }, ts: 0 },
    ]
    // 回放缓冲仅含非 transient 事件（无 tool_call_delta）
    const replay: AgentEvent[] = withDelta.filter((e) => e.type !== 'tool_call_delta')
    const live = toolBlocks(buildChatModel(withDelta).blocks)[0]
    const repl = toolBlocks(buildChatModel(replay).blocks)[0]
    expect(repl.args).toEqual(live.args)
    expect(repl.result).toEqual(live.result)
    // delta 仅在实时路径存在，回放无预览
    expect(live.deltaArgs.length).toBeGreaterThan(0)
    expect(repl.deltaArgs).toBe('')
    expect(repl.toolCallId).toBe('c1')
  })

  it('USER / ERROR / FINAL 各自成块', () => {
    const events: AgentEvent[] = [
      { seq: 0, type: 'user', text: 'do x', ts: 0 },
      { seq: 1, type: 'error', error: 'boom', ts: 0 },
      { seq: 2, type: 'final', text: 'done', ts: 0 },
    ]
    const { blocks } = buildChatModel(events)
    expect(flatten(blocks).map((b) => b.type)).toEqual(['user', 'error', 'text'])
    const finalText = textBlocks(blocks)[0]
    expect(finalText.content).toBe('done')
    expect(finalText.final).toBe(true)
  })

  it('FINAL 在流式场景下不重复（仅收尾，文本已由 TEXT 提供）', () => {
    const events: AgentEvent[] = [
      { seq: 0, type: 'text', text: 'already', kind: 'content', ts: 0 },
      { seq: 1, type: 'final', text: 'already', ts: 0 },
    ]
    const texts = textBlocks(buildChatModel(events).blocks)
    expect(texts).toHaveLength(1)
    expect(texts[0].content).toBe('already')
  })

  it('spawn_subagent：TOOL_USE 在子 agent 前、TOOL_RESULT 在子 agent 后，跨段配对到同一块', () => {
    const sub = 'sess/sub_explore_1_abc123'
    const events: AgentEvent[] = [
      { seq: 0, type: 'tool_call_delta', tc_index: 0, tc_name: 'spawn_subagent', tc_args: '{"agent"', ts: 0 },
      { seq: 1, type: 'tool_use', tool_use: { id: 'c1', name: 'spawn_subagent', arguments: { agent: 'explore', task: 'x' } }, ts: 0 },
      // 子 agent 段（subsession_id 非空）
      { seq: 2, type: 'text', text: '子 agent 思考', kind: 'content', subsession_id: sub, ts: 0 },
      { seq: 3, type: 'final', text: '子 agent 结论', subsession_id: sub, ts: 0 },
      // 父会话结果（回到 subsession_id 空）
      { seq: 4, type: 'tool_result', tool_call_id: 'c1', tool_result: { ok: true, output: '[Subagent explore]\n子 agent 结论' }, ts: 0 },
    ]
    const { blocks } = buildChatModel(events)
    // 不应有「孤立」的工具结果块：spawn_subagent 这一块就带结果且已完成。
    const tools = toolBlocks(blocks)
    expect(tools).toHaveLength(1)
    expect(tools[0].name).toBe('spawn_subagent')
    expect(tools[0].running).toBe(false)
    expect(tools[0].result).toEqual({ ok: true, output: '[Subagent explore]\n子 agent 结论' })
    // 子 agent 独立成块
    const subs = subBlocks(blocks)
    expect(subs).toHaveLength(1)
    expect(subs[0].name).toBe('explore')
    expect(deriveSubagentStatus(subs[0].blocks)).toBe('done')
  })

  it('Bug4：decision 携带文本后紧跟 text 时归并为同一气泡（不重复助手头像）', () => {
    // 复现「子 agent 之后多出头像」：模型先发 decision（含 reasoning 文本）再发 text，
    // 旧逻辑会各成一块导致重复头像；修复后归并到同一气泡。
    const events: AgentEvent[] = [
      { seq: 0, type: 'decision', decision: { text: '我先汇总一下', tool_calls: [] }, ts: 0 },
      { seq: 1, type: 'text', text: '结论如下', kind: 'content', ts: 0 },
      { seq: 2, type: 'final', text: '结论如下', ts: 0 },
    ]
    const texts = textBlocks(buildChatModel(events).blocks)
    expect(texts).toHaveLength(1)
    expect(texts[0].content).toBe('我先汇总一下结论如下')
  })

  it('Bug4：子 agent 段后的父层总结 text 与 subagent 同属一个响应组（共享头像，subagent 卡完整）', () => {
    const sub = 'sess/sub_explore_1_abc123'
    const events: AgentEvent[] = [
      { seq: 0, type: 'tool_call_delta', tc_index: 0, tc_name: 'spawn_subagent', tc_args: '{"agent"', ts: 0 },
      { seq: 1, type: 'tool_use', tool_use: { id: 'c1', name: 'spawn_subagent', arguments: { agent: 'explore', task: 't' } }, ts: 0 },
      // 子 agent 段（subsession_id 非空）
      { seq: 2, type: 'text', text: '子 agent 正在探索', kind: 'content', subsession_id: sub, ts: 0 },
      { seq: 3, type: 'final', text: '子 agent 结论', subsession_id: sub, ts: 0 },
      // 段后父层结果回填（spawn 工具块）
      { seq: 4, type: 'tool_result', tool_call_id: 'c1', tool_result: { ok: true, output: '子 agent 结论' }, ts: 0 },
      // 父 LLM 对子 agent 的总结（紧邻 subagent 段后，此前会作为独立助手头像气泡出现）
      { seq: 5, type: 'text', text: '子 agent 返回结果：1+1=2', kind: 'content', ts: 0 },
      { seq: 6, type: 'final', text: '子 agent 返回结果：1+1=2', ts: 0 },
      // 下一轮 user（轮次结束信号）
      { seq: 7, type: 'user', text: '好的', ts: 0 },
    ]
    const { blocks } = buildChatModel(events)
    // 顶层结构：一个响应组（含 spawn 工具、子 agent 卡、父层总结 text）+ 一个 user 分隔块。
    expect(blocks.map((b) => b.type)).toEqual(['response', 'user'])
    const resp = blocks.find((b) => b.type === 'response') as ResponseBlock
    // 组内顺序：spawn 工具 → 子 agent 卡 → 父层总结 text（三者同轮、共享一个助手头像）。
    expect(resp.blocks.map((b) => b.type)).toEqual(['tool', 'subagent', 'text'])
    // 父层总结 text 确实在组内（与 subagent 同组、共享头像），而非顶层独立头像。
    const summary = resp.blocks.find(
      (b) => b.type === 'text' && (b as { content?: string }).content?.includes('返回结果'),
    )
    expect(summary).toBeDefined()
    // 子 agent 卡保持完整：内部不含「返回结果」总结，且状态推导正常（未被拆开）。
    const subBlock = resp.blocks.find((b) => b.type === 'subagent') as SubagentBlock
    expect(
      subBlock.blocks.some((b) => b.type === 'text' && (b as { content?: string }).content?.includes('返回结果')),
    ).toBe(false)
    expect(deriveSubagentStatus(subBlock.blocks)).toBe('done')
  })

  it('ask_clarification 不会留下悬空「运行中」工具块（被澄清事件丢弃）', () => {
    const events: AgentEvent[] = [
      { seq: 0, type: 'tool_call_delta', tc_index: 0, tc_name: 'ask_clarification', tc_args: '{"questions":[{"question":"?"}]}', ts: 0 },
      { seq: 1, type: 'clarify', questions: [{ question: '想澄清什么？', options: ['A', 'B'] }], ts: 0 },
    ]
    const { blocks } = buildChatModel(events)
    // 没有 tool 块残留（delta 产生的悬空块被丢弃），只有澄清块。
    expect(toolBlocks(blocks)).toHaveLength(0)
    const clarifies = flatten(blocks).filter((b) => b.type === 'clarify')
    expect(clarifies).toHaveLength(1)
  })

  it('澄清回答回填到澄清块（与澄清一起渲染，不另起 user 块）', () => {
    const events: AgentEvent[] = [
      { seq: 0, type: 'clarify', questions: [{ question: '想澄清什么？' }], ts: 0 },
      { seq: 1, type: 'user', text: '项目需求不明确', ts: 0 },
    ]
    const { blocks } = buildChatModel(events)
    const clarifies = flatten(blocks).filter((b) => b.type === 'clarify')
    expect(clarifies).toHaveLength(1)
    expect(clarifies[0].type === 'clarify' && clarifies[0].answer).toBe('项目需求不明确')
    // 回答未另成 user 块
    expect(flatten(blocks).filter((b) => b.type === 'user')).toHaveLength(0)
  })

  it('M10.4：USAGE 事件累积到 ResponseBlock.turnMeta（单轮）', () => {
    const events: AgentEvent[] = [
      // 轮次①：用户消息 → 助理回答 → USAGE（父级）
      { seq: 0, type: 'user', text: '你好', ts: 0 },
      { seq: 1, type: 'text', text: 'Hi', kind: 'content', ts: 0 },
      { seq: 2, type: 'final', text: 'Hi', ts: 0 },
      { seq: 3, type: 'usage', message_id: 'm1', usage: { total_tokens: 10, prompt_tokens: 5, completion_tokens: 5 }, duration: 2.1, estimated: false, ts: 0 },
    ]
    const { blocks } = buildChatModel(events)
    const resp = blocks.find((b) => b.type === 'response') as ResponseBlock
    expect(resp).toBeDefined()
    expect(resp.turnMeta).toBeDefined()
    expect(resp.turnMeta?.usage.total_tokens).toBe(10)
    expect(resp.turnMeta?.duration).toBeCloseTo(2.1, 1)
  })

  it('M10.4：子 agent 段内的 USAGE 归集到父 ResponseBlock', () => {
    const sub = 'sess/sub_explore_1_abc123'
    const events: AgentEvent[] = [
      { seq: 0, type: 'user', text: '查询', ts: 0 },
      { seq: 1, type: 'text', text: '探索中', kind: 'content', ts: 0 },
      { seq: 2, type: 'tool_use', tool_use: { id: 'c1', name: 'spawn_subagent', arguments: { agent: 'explore' } }, ts: 0 },
      // 子 agent 段（含 USAGE）
      { seq: 3, type: 'text', text: '子 agent 思考', kind: 'content', subsession_id: sub, ts: 0 },
      { seq: 4, type: 'final', text: '子 agent 结论', subsession_id: sub, ts: 0 },
      // 子 agent 的 USAGE 事件（携带 parent_message_id 指向 m1）
      { seq: 5, type: 'usage', message_id: 'sub-m1', parent_message_id: 'm1', usage: { total_tokens: 20, prompt_tokens: 10, completion_tokens: 10 }, duration: 3.5, estimated: false, subsession_id: sub, ts: 0 },
      // 父层结果
      { seq: 6, type: 'tool_result', tool_call_id: 'c1', tool_result: { ok: true, output: '结果' }, ts: 0 },
      { seq: 7, type: 'final', text: '总结', ts: 0 },
      // 父级 USAGE
      { seq: 8, type: 'usage', message_id: 'm2', usage: { total_tokens: 15, prompt_tokens: 7, completion_tokens: 8 }, duration: 1.5, estimated: false, ts: 0 },
    ]
    const { blocks } = buildChatModel(events)
    const resp = blocks.find((b) => b.type === 'response') as ResponseBlock
    expect(resp).toBeDefined()
    expect(resp.turnMeta).toBeDefined()
    // 子 agent 20 + 父级 15 = 35
    expect(resp.turnMeta?.usage.total_tokens).toBe(35)
    // 子 agent 3.5 + 父级 1.5 = 5.0
    expect(resp.turnMeta?.duration).toBeCloseTo(5.0, 1)
  })

  it('M11：后台 subsession（session-memory）的 USAGE 不计入前台用量', () => {
    const sub = 'sess/sub_session-memory_0_3bb340'
    const events: AgentEvent[] = [
      { seq: 0, type: 'user', text: '写文章', ts: 0 },
      { seq: 1, type: 'text', text: '写作中', kind: 'content', ts: 0 },
      // 后台记忆子 agent：带 USAGE（background=true），其 token 消耗不应计入前台
      { seq: 2, type: 'text', text: '记忆内容', kind: 'content', subsession_id: sub, background: true, ts: 0 },
      { seq: 3, type: 'final', text: '记忆完成', subsession_id: sub, background: true, ts: 0 },
      { seq: 4, type: 'usage', message_id: 'sub-m1', usage: { total_tokens: 50, prompt_tokens: 30, completion_tokens: 20 }, duration: 5.0, estimated: false, subsession_id: sub, background: true, ts: 0 },
      // 父级回答 + USAGE
      { seq: 5, type: 'final', text: '写完了', ts: 0 },
      { seq: 6, type: 'usage', message_id: 'm2', usage: { total_tokens: 10, prompt_tokens: 6, completion_tokens: 4 }, duration: 1.0, estimated: false, ts: 0 },
    ]
    const { blocks } = buildChatModel(events)
    // 后台子 agent 不渲染成前台卡
    expect(subBlocks(blocks)).toHaveLength(0)
    const resp = blocks.find((b) => b.type === 'response') as ResponseBlock
    // 前台用量只含父级 10，不含后台子 agent 的 50
    expect(resp.turnMeta?.usage.total_tokens).toBe(10)
    expect(resp.turnMeta?.duration).toBeCloseTo(1.0, 1)
  })

  it('正常 user 任务不误并入澄清块', () => {
    const events: AgentEvent[] = [
      { seq: 0, type: 'user', text: '新任务', ts: 0 },
      { seq: 1, type: 'clarify', questions: [{ question: '?' }], ts: 0 },
    ]
    const { blocks } = buildChatModel(events)
    expect(flatten(blocks).filter((b) => b.type === 'user')).toHaveLength(1)
    expect(flatten(blocks).filter((b) => b.type === 'clarify')).toHaveLength(1)
  })

  it('运行中的子 agent 推导为 running', () => {
    const sub = 'sess/sub_explore_1_abc123'
    const events: AgentEvent[] = [
      { seq: 0, type: 'text', text: '子 agent 正在思考', kind: 'content', subsession_id: sub, ts: 0 },
    ]
    const subs = subBlocks(buildChatModel(events).blocks)
    expect(subs).toHaveLength(1)
    expect(deriveSubagentStatus(subs[0].blocks)).toBe('running')
  })

  it('流式标记：最后一个未收尾文本块 streaming=true，被工具冲刷出的中间思考段 streaming=false', () => {
    const events: AgentEvent[] = [
      // 轮次①：纯思考后调用工具（该思考块应被冲刷、streaming=false）
      { seq: 0, type: 'text', text: '我需要先看看', kind: 'reasoning', ts: 0 },
      { seq: 1, type: 'tool_call_delta', tc_index: 0, tc_name: 'read', tc_args: '{"path":', ts: 0 },
      { seq: 2, type: 'tool_use', tool_use: { id: 't1', name: 'read', arguments: { path: 'x' } }, ts: 0 },
      { seq: 3, type: 'tool_result', tool_call_id: 't1', tool_result: { ok: true, output: '...' }, ts: 0 },
      // 轮次②：当前正在流式生成（无 final）→ 最后一个文本块 streaming=true
      { seq: 4, type: 'text', text: '我还在想', kind: 'reasoning', ts: 0 },
    ]
    const texts = textBlocks(buildChatModel(events).blocks)
    expect(texts).toHaveLength(2)
    expect(texts[0].streaming).toBe(false) // 被工具冲刷出的中间思考段
    expect(texts[0].final).toBe(false)
    expect(texts[1].streaming).toBe(true) // 当前活动气泡
  })

  it('终态收尾的文本块 streaming=false（完整回放不应残留光标）', () => {
    const events: AgentEvent[] = [
      { seq: 0, type: 'text', text: '结论', kind: 'content', ts: 0 },
      { seq: 1, type: 'final', text: '结论', ts: 0 },
    ]
    const texts = textBlocks(buildChatModel(events).blocks)
    expect(texts).toHaveLength(1)
    expect(texts[0].streaming).toBe(false)
    expect(texts[0].final).toBe(true)
  })

  it('子 agent 以 final 终态收尾 → 推导为 done（显示已完成）', () => {
    const sub = 'sess/sub_explore_1_abc123'
    const events: AgentEvent[] = [
      { seq: 0, type: 'text', text: '探索中', kind: 'reasoning', subsession_id: sub, ts: 0 },
      { seq: 1, type: 'text', text: '结论文本', kind: 'content', subsession_id: sub, ts: 0 },
      { seq: 2, type: 'final', text: '结论文本', subsession_id: sub, ts: 0 },
    ]
    const subs = subBlocks(buildChatModel(events).blocks)
    expect(subs).toHaveLength(1)
    expect(deriveSubagentStatus(subs[0].blocks)).toBe('done')
  })

  it('Bug5：同一 subsession 被父事件隔成多段时合并为单一子 agent 块（不产生重复 key）', () => {
    // 复现 session-memory 子 agent：同一 sub 的事件流被父会话事件（如 usage）隔成两段。
    // 若分别 push 两个 `sub-<id>` 块 → 重复 key → React 渲染错乱。
    const sub = 'sess/sub_session-memory_0_3bb340'
    const events: AgentEvent[] = [
      { seq: 0, type: 'text', text: '记忆开始', kind: 'content', subsession_id: sub, ts: 0 },
      // 父事件（usage）插在中间，把同一子 agent 段隔开
      { seq: 1, type: 'usage', usage: { prompt_tokens: 10, completion_tokens: 5 }, ts: 0 },
      { seq: 2, type: 'text', text: '记忆继续', kind: 'content', subsession_id: sub, ts: 0 },
      { seq: 3, type: 'final', text: '记忆完成', subsession_id: sub, ts: 0 },
    ]
    const { blocks } = buildChatModel(events)
    // 关键：同一 sub 只应产生一个子 agent 块（两段合并）
    const subs = subBlocks(blocks)
    expect(subs).toHaveLength(1)
    expect(subs[0].key).toBe(`sub-${sub}`)
    // 两段文本都应合并进该块（final 仅收尾，不产生重复文本）
    const textContents = subs[0].blocks
      .filter((b) => b.type === 'text')
      .map((b) => (b as { content?: string }).content ?? '')
    expect(textContents.join('|')).toContain('记忆开始')
    expect(textContents.join('|')).toContain('记忆继续')
  })

  it('M11：后台 subsession（session-memory）事件不渲染成前台子 agent 卡', () => {
    const sub = 'sess/sub_session-memory_0_3bb340'
    const events: AgentEvent[] = [
      // 后台记忆子 agent：background=true，其事件不应出现在前台聊天区
      { seq: 0, type: 'text', text: '记忆内容', kind: 'content', subsession_id: sub, background: true, ts: 0 },
      { seq: 1, type: 'final', text: '记忆完成', subsession_id: sub, background: true, ts: 0 },
    ]
    const { blocks } = buildChatModel(events)
    // 前台不应产生任何 subagent 卡
    expect(subBlocks(blocks)).toHaveLength(0)
  })
})
