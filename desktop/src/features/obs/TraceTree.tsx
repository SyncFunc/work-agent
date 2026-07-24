// TraceTree：列出当前项目的 trace，选中后展示可折叠的 span 父子树。

import { useEffect, useMemo, useState } from 'react'
import { DaemonClient } from '../../protocol/client'
import type { SpanNode, TraceInfo } from '../../protocol/types'
import { getTrace, listTraces } from './obsApi'
import { buildTree, type SpanTreeNode } from './traceTree'

interface Props {
  client: DaemonClient | null
  projectRoot: string
  sessionId: string | null
}

function durationMs(s: SpanNode): number {
  const end = s.ended_at ?? Date.now() / 1000
  return Math.max(0, (end - s.started_at) * 1000)
}

export function TraceTree({ client, projectRoot, sessionId }: Props) {
  const [traces, setTraces] = useState<TraceInfo[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [spans, setSpans] = useState<SpanNode[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())

  useEffect(() => {
    if (!client || !projectRoot) return
    let cancelled = false
    setLoading(true)
    setError(null)
    listTraces(client, projectRoot)
      .then((resp) => {
        if (cancelled) return
        setTraces(resp.traces)
        const pick = resp.traces.find((t) => t.session_id === sessionId)?.session_id
        setSelected(pick ?? resp.traces[0]?.session_id ?? null)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(String(e))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [client, projectRoot, sessionId])

  useEffect(() => {
    if (!client || !projectRoot || !selected) {
      setSpans([])
      return
    }
    let cancelled = false
    getTrace(client, projectRoot, selected)
      .then((resp) => {
        if (!cancelled) setSpans(resp.spans)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(String(e))
      })
    return () => {
      cancelled = true
    }
  }, [client, projectRoot, selected])

  const tree = useMemo(() => buildTree(spans), [spans])

  const toggle = (id: string): void => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ display: 'flex', gap: 6, alignItems: 'center', padding: '4px 8px' }}>
        <span style={{ fontSize: 12, color: '#666' }}>Trace</span>
        <select
          value={selected ?? ''}
          onChange={(e) => setSelected(e.target.value || null)}
          style={{ flex: 1, fontSize: 12 }}
          disabled={traces.length === 0}
        >
          {traces.length === 0 && <option value="">（无 trace）</option>}
          {traces.map((t) => (
            <option key={t.session_id} value={t.session_id}>
              {t.session_id.slice(0, 8)} · {t.span_count} spans
            </option>
          ))}
        </select>
        <button type="button" onClick={() => selected && getTrace(client as DaemonClient, projectRoot, selected).then((r) => setSpans(r.spans)).catch(() => {})}>
          刷新
        </button>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', padding: '4px 8px', fontSize: 12 }}>
        {loading && <p style={{ color: '#999' }}>加载中…</p>}
        {error && <p style={{ color: 'crimson' }}>错误：{error}</p>}
        {!loading && !error && tree.length === 0 && <p style={{ color: '#999' }}>暂无 span</p>}
        {tree.map((node) => (
          <TreeNodeView key={node.span.span_id} node={node} depth={0} collapsed={collapsed} onToggle={toggle} />
        ))}
      </div>
    </div>
  )
}

function TreeNodeView({
  node,
  depth,
  collapsed,
  onToggle,
}: {
  node: SpanTreeNode
  depth: number
  collapsed: Set<string>
  onToggle: (id: string) => void
}) {
  const isCollapsed = collapsed.has(node.span.span_id)
  const hasChildren = node.children.length > 0
  const statusColor = node.span.status === 'open' ? '#f29900' : '#1e7e34'
  return (
    <div style={{ marginLeft: depth * 14 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '1px 0' }}>
        {hasChildren ? (
          <button
            type="button"
            onClick={() => onToggle(node.span.span_id)}
            style={{ border: 'none', background: 'transparent', cursor: 'pointer', fontSize: 11, width: 16 }}
          >
            {isCollapsed ? '▸' : '▾'}
          </button>
        ) : (
          <span style={{ width: 16, display: 'inline-block' }} />
        )}
        <span style={{ fontWeight: 600 }}>{node.span.name}</span>
        <span style={{ color: '#999' }}>[{node.span.kind}]</span>
        <span style={{ color: statusColor }}>· {node.span.status}</span>
        <span style={{ color: '#999' }}>{durationMs(node.span).toFixed(1)}ms</span>
        {node.span.logs.length > 0 && (
          <span style={{ color: '#999' }}>· {node.span.logs.length} 日志</span>
        )}
      </div>
      {!isCollapsed &&
        node.children.map((c) => (
          <TreeNodeView
            key={c.span.span_id}
            node={c}
            depth={depth + 1}
            collapsed={collapsed}
            onToggle={onToggle}
          />
        ))}
    </div>
  )
}
