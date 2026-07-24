import { describe, expect, it } from 'vitest'
import { mergeSettings } from '../../main/settings'

describe('mergeSettings', () => {
  it('深合并嵌套对象（仅覆盖 patch 中的键）', () => {
    const base = { llm: { model: 'a', base_url: 'u' }, plan: { mode: 'x' } }
    const patch = { llm: { api_key: 'k' } }
    expect(mergeSettings(base, patch)).toEqual({
      llm: { model: 'a', base_url: 'u', api_key: 'k' },
      plan: { mode: 'x' },
    })
  })

  it('标量整体替换', () => {
    expect(mergeSettings({ a: 1 }, { a: 2 })).toEqual({ a: 2 })
  })

  it('数组整体替换（不逐元素合并）', () => {
    expect(mergeSettings({ x: [1, 2] }, { x: [3] })).toEqual({ x: [3] })
  })

  it('不修改入参', () => {
    const base = { llm: { model: 'a' } }
    const patch = { llm: { model: 'b' } }
    mergeSettings(base, patch)
    expect(base.llm.model).toBe('a')
  })
})
