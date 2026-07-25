// StatusBar：底部状态栏——上下文 token 用量、窗口占比、当前模式、项目根、会话 id。

import type { UsagePayload } from '../../protocol/types'
import type { ObsMode } from './useObs'

interface Props {
  projectRoot: string
  sessionId: string | null
  usage: UsagePayload['usage'] | null
  estimated: boolean
  mode: ObsMode
  contextWindow?: number
}

function num(v: number | undefined): number {
  return typeof v === 'number' ? v : 0
}

export function StatusBar({ projectRoot, sessionId, usage, estimated, mode, contextWindow }: Props) {
  const prompt = num(usage?.prompt_tokens)
  const completion = num(usage?.completion_tokens)
  const total = num(usage?.total_tokens) || num(usage?.estimated_tokens)
  const pct =
    contextWindow && contextWindow > 0 ? Math.min(100, (total / contextWindow) * 100) : null

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 14,
        padding: '6px 10px',
        fontSize: 12,
        borderBottom: '1px solid #eee',
        background: '#fafafa',
        flexWrap: 'wrap',
      }}
    >
      <span
        style={{
          padding: '1px 6px',
          borderRadius: 4,
          background: mode === 'plan' ? '#fff4d6' : '#e6f4ea',
          color: mode === 'plan' ? '#8a6d00' : '#1e7e34',
          fontWeight: 600,
        }}
      >
        {mode === 'plan' ? 'PLAN' : 'EXEC'}
      </span>
      <span title="prompt tokens">prompt {prompt}</span>
      <span title="completion tokens">completion {completion}</span>
      <span title="total tokens">
        total {total}
        {estimated ? ' (估算)' : ''}
      </span>
      {pct !== null && (
        <span title={`窗口占比 ${pct.toFixed(1)}%`} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <span
            style={{
              display: 'inline-block',
              width: 60,
              height: 6,
              borderRadius: 3,
              background: '#e0e0e0',
              overflow: 'hidden',
            }}
          >
            <span
              style={{
                display: 'block',
                height: '100%',
                width: `${pct}%`,
                background: pct > 80 ? '#d93025' : pct > 60 ? '#f29900' : '#1a73e8',
              }}
            />
          </span>
          {pct.toFixed(0)}%
        </span>
      )}
      <span
        style={{
          marginLeft: 'auto',
          color: '#666',
          maxWidth: 220,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
        title={projectRoot}
      >
        {projectRoot}
      </span>
      {sessionId && (
        <span style={{ color: '#666' }} title={sessionId}>
          #{sessionId.slice(0, 8)}
        </span>
      )}
    </div>
  )
}
