import { describe, expect, it } from 'vitest'
import { computeThrottledTarget, computeUnifiedDiff, extractPartialContent, extractPartialPath } from './diff'

describe('diff utils partial extraction (真实流式片段)', () => {
  it('累积片段：path + content 首行', () => {
    const args = '{"path":"src/a.py","content":"import os\\n'
    expect(extractPartialPath(args)).toBe('src/a.py')
    expect(extractPartialContent(args)).toBe('import os\n')
  })

  it('完整 JSON', () => {
    const args = '{"path":"src/a.py","content":"import os\\ndef h():"}'
    expect(extractPartialPath(args)).toBe('src/a.py')
    expect(extractPartialContent(args)).toBe('import os\ndef h():')
  })

  it('content 含引号转义', () => {
    const args = '{"path":"x.py","content":"print(\\"hi\\")\\n"}'
    expect(extractPartialContent(args)).toBe('print("hi")\n')
  })

  it('path 缺失（无 path 字段）', () => {
    const args = '{"content":"only content"}'
    expect(extractPartialPath(args)).toBe(null)
    expect(extractPartialContent(args)).toBe('only content')
  })
})

describe('computeUnifiedDiff 空 original（新建文件）', () => {
  it('original 为空串时也能算出全新增行的 diff', () => {
    const diff = computeUnifiedDiff('', 'import os\nprint(1)\n', '桃花源记.md')
    expect(diff).toContain('+import os')
    expect(diff).toContain('+print(1)')
  })
})

describe('computeThrottledTarget 流式目标内容', () => {
  it('流式有内容（含换行）即更新 target', () => {
    const r = computeThrottledTarget({
      fullContent: undefined,
      streamingContent: 'import os\n',
      lastTarget: '',
    })
    expect(r.target).toBe('import os\n')
  })

  it('流式不完整行（无换行）也实时更新（不等待）', () => {
    const r = computeThrottledTarget({
      fullContent: undefined,
      streamingContent: 'import os',
      lastTarget: '',
    })
    expect(r.target).toBe('import os')
  })

  it('流式第一阶段（无内容）返回空', () => {
    const r = computeThrottledTarget({
      fullContent: undefined,
      streamingContent: null,
      lastTarget: '',
    })
    expect(r.target).toBe('')
  })

  it('完整 content（tool_use 后）始终最新', () => {
    const r = computeThrottledTarget({
      fullContent: 'import os\ndef h():\n',
      streamingContent: null,
      lastTarget: 'import os\n',
    })
    expect(r.target).toBe('import os\ndef h():\n')
  })
})
