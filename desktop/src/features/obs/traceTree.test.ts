import { describe, expect, it } from 'vitest'
import { buildTree, flattenTree } from './traceTree'
import type { SpanNode } from '../../protocol/types'

function span(span_id: string, parent_id: string | null): SpanNode {
  return {
    span_id,
    name: span_id,
    kind: 'span',
    parent_id,
    started_at: 0,
    ended_at: 1,
    status: 'ok',
    meta: {},
    logs: [],
  }
}

describe('buildTree', () => {
  it('按 parent_id 重建父子层级', () => {
    const spans = [
      span('root', null),
      span('a', 'root'),
      span('b', 'root'),
      span('a1', 'a'),
    ]
    const tree = buildTree(spans)
    expect(tree).toHaveLength(1)
    const root = tree[0]
    expect(root.span.span_id).toBe('root')
    expect(root.children.map((c) => c.span.span_id)).toEqual(['a', 'b'])
    const a = root.children[0]
    expect(a.children.map((c) => c.span.span_id)).toEqual(['a1'])
  })

  it('悬空 parent_id 视为根', () => {
    const spans = [span('x', 'missing'), span('y', null)]
    const tree = buildTree(spans)
    expect(tree.map((n) => n.span.span_id).sort()).toEqual(['x', 'y'])
  })

  it('空列表返回空树', () => {
    expect(buildTree([])).toEqual([])
  })

  it('flattenTree 深度优先顺序', () => {
    const spans = [span('root', null), span('a', 'root'), span('a1', 'a')]
    const flat = flattenTree(buildTree(spans))
    expect(flat.map((n) => n.span.span_id)).toEqual(['root', 'a', 'a1'])
  })
})
