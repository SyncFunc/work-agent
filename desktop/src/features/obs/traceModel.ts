// 纯函数：把扁平的 span 列表（含 parent_id）重建为父子树。无副作用，可单测。

import type { SpanNode } from '../../protocol/types'

export interface SpanTreeNode {
  span: SpanNode
  children: SpanTreeNode[]
}

export function buildTree(spans: SpanNode[]): SpanTreeNode[] {
  const nodes = new Map<string, SpanTreeNode>()
  for (const s of spans) nodes.set(s.span_id, { span: s, children: [] })

  const roots: SpanTreeNode[] = []
  for (const s of spans) {
    const node = nodes.get(s.span_id)
    if (node === undefined) continue
    const parent = s.parent_id ? nodes.get(s.parent_id) : undefined
    // parent 不存在（悬空引用）→ 视为根
    if (parent && parent !== node) {
      parent.children.push(node)
    } else {
      roots.push(node)
    }
  }
  return roots
}

/** 树深度优先扁平化（用于可折叠渲染时的顺序遍历）。 */
export function flattenTree(roots: SpanTreeNode[]): SpanTreeNode[] {
  const out: SpanTreeNode[] = []
  const walk = (n: SpanTreeNode): void => {
    out.push(n)
    for (const c of n.children) walk(c)
  }
  for (const r of roots) walk(r)
  return out
}
