import { describe, expect, it } from 'vitest'
import { buildChatModel, type ChatBlock } from './useEventReducer'
import type { AgentEvent } from '../../protocol/types'

function toolBlocks(blocks: ChatBlock[]) {
  return blocks.filter((b): b is Extract<ChatBlock, { type: 'tool' }> => b.type === 'tool')
}
function textBlocks(blocks: ChatBlock[]) {
  return blocks.filter((b): b is Extract<ChatBlock, { type: 'text' }> => b.type === 'text')
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
      { seq: 1, type: 'tool_call_delta', tc_index: 0, tc_name: 'bash', tc_args: '{"cmd"', ts: 0 },
      { seq: 2, type: 'tool_call_delta', tc_index: 0, tc_args: ':"ls"}', ts: 0 },
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
    // 文本块在工具块之前（顺序正确）
    expect(blocks[0].type).toBe('text')
    expect(blocks[1].type).toBe('tool')
  })

  it('replay 一致性：不含 transient delta 的回放，工具最终参数/结果与带 delta 的实时一致', () => {
    const withDelta: AgentEvent[] = [
      { seq: 0, type: 'tool_call_delta', tc_index: 0, tc_name: 'bash', tc_args: '{"cmd"', ts: 0 },
      { seq: 1, type: 'tool_call_delta', tc_index: 0, tc_args: ':"ls"}', ts: 0 },
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
    expect(blocks.map((b) => b.type)).toEqual(['user', 'error', 'text'])
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
})
