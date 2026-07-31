import { describe, expect, it } from 'vitest'
import { extractPartialContent } from './diff'

describe('extractPartialContent 古诗文真实片段', () => {
  it('content 含换行转义 + 中文 + 标点（流式片段）', () => {
    const args = '{"content": "# 出师表\\n\\n**三国蜀·诸葛亮**\\n\\n先帝创业未半而中道崩殂，今天下三分，益州疲'
    const content = extractPartialContent(args)
    expect(content).not.toBeNull()
    expect(content).toContain('# 出师表')
    expect(content).toContain('**三国蜀·诸葛亮**')
    expect(content).toContain('先帝创业未半')
  })

  it('content 完整闭合 JSON（含 path 在 content 后）', () => {
    const args = '{"content": "# 出师表\\n先帝创业\\n", "path": "出师表.md"}'
    const content = extractPartialContent(args)
    expect(content).toBe('# 出师表\n先帝创业\n')
  })

  it('content 值内含 \\" 转义引号', () => {
    const args = '{"content": "他说：\\"你好\\"\\n结束"}'
    const content = extractPartialContent(args)
    expect(content).toBe('他说："你好"\n结束')
  })
})
