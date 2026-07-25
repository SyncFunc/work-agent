import { describe, expect, it } from 'vitest'
import { parseSlash } from './parseSlash'

describe('parseSlash', () => {
  it('非斜杠输入返回 null', () => {
    expect(parseSlash('hello')).toBeNull()
    expect(parseSlash('  ')).toBeNull()
  })

  it('仅命令名', () => {
    expect(parseSlash('/compact')).toEqual({ name: 'compact', args: '' })
  })

  it('命令名 + 参数', () => {
    expect(parseSlash('/plan --mode x')).toEqual({ name: 'plan', args: '--mode x' })
    expect(parseSlash('/skill my-skill')).toEqual({ name: 'skill', args: 'my-skill' })
  })

  it('仅斜杠 + 空格（无命令名）返回 null', () => {
    expect(parseSlash('/  ')).toBeNull()
    expect(parseSlash('/')).toBeNull()
  })
})
