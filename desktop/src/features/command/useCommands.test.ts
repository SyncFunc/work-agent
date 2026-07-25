import { describe, expect, it } from 'vitest'
import { COMMANDS } from './useCommands'

describe('COMMANDS 注册表', () => {
  it('包含 M7.5 关键命令', () => {
    const names = COMMANDS.map((c) => c.name)
    for (const n of ['context', 'compact', 'plan', 'skills', 'agents', 'mode', 'resume', 'fork', 'help']) {
      expect(names).toContain(n)
    }
  })

  it('resume/fork 标记需要会话上下文', () => {
    const resume = COMMANDS.find((c) => c.name === 'resume')
    const fork = COMMANDS.find((c) => c.name === 'fork')
    expect(resume?.needsSession).toBe(true)
    expect(fork?.needsSession).toBe(true)
  })
})
