import type { TraceInfo, SpanNode as SpanNodeType } from '../../protocol/types'
import type { DaemonClient } from '../../protocol/client'
import { getTrace, listTraces } from './obsApi'
import { AlertCircle, CheckCircle2, ChevronRight, Clock, ListTree, RefreshCw } from 'lucide-react'
import { Spinner } from '../../components'
import { useCallback, useEffect, useState } from 'react'
import type { SpanTreeNode } from './traceModel'
import { buildTree } from './traceModel'
import { SessionBar } from './SessionBar'
import { SpanDetail } from './SpanDetail'
import { TraceList } from './TraceList'

interface TracePanelProps {
  client: DaemonClient | null
  projectRoot: string
  sessionId: string | null
}

function SpanNodeView({
  node,
  depth,
  selectedId,
  onSelect,
}: {
  node: SpanTreeNode
  depth: number
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  const [expanded, setExpanded] = useState(true)
  const isSelected = node.span.span_id === selectedId
  const hasChildren = node.children.length > 0

  const statusColorClass =
    node.span.status === 'error'
      ? 'wa-spantree__node-status--error'
      : node.span.status === 'open'
        ? 'wa-spantree__node-status--open'
        : 'wa-spantree__node-status--ok'

  const StatusIcon =
    node.span.status === 'error'
      ? AlertCircle
      : node.span.status === 'open'
        ? Clock
        : CheckCircle2

  const duration =
    node.span.started_at && node.span.ended_at
      ? ((node.span.ended_at - node.span.started_at) * 1000).toFixed(1)
      : null

  return (
    <div>
      <div
        className={`wa-spantree__node ${isSelected ? 'wa-spantree__node--selected' : ''}`}
        style={{ paddingLeft: `${8 + depth * 16}px` }}
        onClick={() => onSelect(node.span.span_id)}
      >
        <div className="wa-spantree__node-row">
          {hasChildren ? (
            <span
              className={`wa-spantree__node-toggle ${expanded ? 'wa-spantree__node-toggle--expanded' : ''}`}
              onClick={(e) => { e.stopPropagation(); setExpanded(!expanded) }}
            >
              <ChevronRight size={12} />
            </span>
          ) : (
            <span style={{ width: 14, flex: 'none' }} />
          )}
          <span className={`wa-spantree__node-icon ${statusColorClass}`}>
            <StatusIcon size={12} />
          </span>
          <span className="wa-spantree__node-name">{node.span.name}</span>
          <span className="wa-spantree__node-kind">{node.span.kind}</span>
          {duration !== null && (
            <span className="wa-spantree__node-duration">
              {parseFloat(duration) >= 1000
                ? `${(parseFloat(duration) / 1000).toFixed(2)}s`
                : `${duration}ms`}
            </span>
          )}
        </div>
      </div>
      {hasChildren && expanded && (
        <div className="wa-spantree__children">
          {node.children.map((child) => (
            <SpanNodeView key={child.span.span_id} node={child} depth={depth + 1} selectedId={selectedId} onSelect={onSelect} />
          ))}
        </div>
      )}
    </div>
  )
}

export function TracePanel({ client, projectRoot, sessionId }: TracePanelProps) {
  const [traces, setTraces] = useState<TraceInfo[]>([])
  const [activeSessionId, setActiveSessionId] = useState<string | null>(sessionId)
  const [activeTraceId, setActiveTraceId] = useState<string | null>(null)
  const [spans, setSpans] = useState<SpanNodeType[]>([])
  const [tree, setTree] = useState<SpanTreeNode[]>([])
  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [traceLoading, setTraceLoading] = useState(false)
  const [page, setPage] = useState(1)
  const pageSize = 15

  // 从 traces 中提取去重 session
  const sessions: { session_id: string; trace_count: number; span_count: number }[] = []
  const sessionMap = new Map<string, { session_id: string; trace_count: number; span_count: number }>()
  for (const t of traces) {
    let s = sessionMap.get(t.session_id)
    if (!s) {
      s = { session_id: t.session_id, trace_count: 0, span_count: 0 }
      sessionMap.set(t.session_id, s)
    }
    s.trace_count++
    s.span_count += t.span_count
  }
  sessions.push(...sessionMap.values())
  sessions.sort((a, b) => b.span_count - a.span_count)

  // 当前 session 下的 trace 列表
  const sessionTraces = traces.filter((t) => t.session_id === activeSessionId)
  const totalPages = Math.max(1, Math.ceil(sessionTraces.length / pageSize))
  const pageTraces = sessionTraces.slice((page - 1) * pageSize, page * pageSize)

  // 加载 trace 列表
  const loadTraces = useCallback(async () => {
    if (!client) return
    setLoading(true)
    try {
      const resp = await listTraces(client, projectRoot)
      if (resp) {
        setTraces(resp.traces ?? [])
        if (!activeSessionId && resp.traces.length > 0) {
          setActiveSessionId(resp.traces[0].session_id)
        }
      }
    } finally {
      setLoading(false)
    }
  }, [client, projectRoot])

  useEffect(() => {
    loadTraces()
  }, [loadTraces])

  // 选中 trace 时加载 span 树
  useEffect(() => {
    if (!activeTraceId || !client) return
    setTraceLoading(true)
    setSelectedSpanId(null)
    getTrace(client, projectRoot, activeTraceId)
      .then((resp) => {
        if (resp) {
          setSpans(resp.spans ?? [])
        }
      })
      .finally(() => setTraceLoading(false))
  }, [client, projectRoot, activeTraceId])

  // 构建 span 树
  useEffect(() => {
    setTree(spans.length === 0 ? [] : buildTree(spans))
  }, [spans])

  // session 切换时重置分页和选中
  const handleSessionChange = useCallback((sid: string) => {
    setActiveSessionId(sid)
    setPage(1)
    setActiveTraceId(null)
    setSpans([])
    setTree([])
    setSelectedSpanId(null)
  }, [])

  // trace 选中
  const handleTraceSelect = useCallback((traceId: string) => {
    setActiveTraceId(traceId)
    setSelectedSpanId(null)
  }, [])

  // 选中 span 详情
  const selectedSpan = selectedSpanId
    ? spans.find((s) => s.span_id === selectedSpanId) ?? null
    : null

  const getTraceIcon = (_trace: TraceInfo): 'ok' | 'error' | 'open' | 'muted' => {
    return 'muted'
  }

  return (
    <div className="wa-tracepanel">
      {/* SessionBar */}
      <SessionBar
        sessions={sessions}
        activeSessionId={activeSessionId}
        onChange={handleSessionChange}
      />

      {/* TraceList */}
      {loading ? (
        <div className="wa-tracelist__empty">
          <Spinner />
        </div>
      ) : (
        <TraceList
          traces={pageTraces}
          totalPages={totalPages}
          page={page}
          activeTraceId={activeTraceId}
          onSelect={handleTraceSelect}
          onPageChange={setPage}
          getIconType={getTraceIcon}
        />
      )}

      {/* Span 树 + 详情 */}
      <div className="wa-traceview">
        <div className="wa-spantree">
          {traceLoading ? (
            <div className="wa-spantree__node" style={{ padding: '12px', textAlign: 'center' }}>
              <Spinner />
            </div>
          ) : tree.length === 0 ? (
            <div className="wa-spandetail__empty">选择一条 trace 查看详情</div>
          ) : (
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '4px 12px', borderBottom: '1px solid var(--wa-border)', marginBottom: 4 }}>
                <ListTree size={12} style={{ color: 'var(--wa-text-muted)' }} />
                <span style={{ fontSize: 'var(--wa-f-xs)', color: 'var(--wa-text-muted)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.5 }}>
                  Span Tree
                </span>
                <RefreshCw
                  size={12}
                  style={{ marginLeft: 'auto', cursor: 'pointer', color: 'var(--wa-text-muted)' }}
                  onClick={() => { if (activeTraceId && client) getTrace(client, projectRoot, activeTraceId).then((r) => r && setSpans(r.spans ?? [])) }}
                />
              </div>
              {tree.map((node) => (
                <SpanNodeView
                  key={node.span.span_id}
                  node={node}
                  depth={0}
                  selectedId={selectedSpanId}
                  onSelect={setSelectedSpanId}
                />
              ))}
            </div>
          )}
        </div>

        <SpanDetail span={selectedSpan} onClose={() => setSelectedSpanId(null)} />
      </div>
    </div>
  )
}
