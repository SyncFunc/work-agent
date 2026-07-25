// StatusBar：底部状态栏——上下文 token 用量、窗口占比、当前模式、项目根、会话 id。

import type { UsagePayload } from '../../protocol/types'
import type { ObsMode } from './useObs'
import { Badge } from '../../components'

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
  const pct = contextWindow && contextWindow > 0 ? Math.min(100, (total / contextWindow) * 100) : null
  const barColor =
    pct == null
      ? 'var(--wa-border-strong)'
      : pct > 80
        ? 'var(--wa-danger)'
        : pct > 60
          ? 'var(--wa-warn)'
          : 'var(--wa-primary)'

  return (
    <div className="wa-statusbar">
      <Badge tone={mode === 'plan' ? 'warn' : 'success'}>{mode === 'plan' ? 'PLAN' : 'EXEC'}</Badge>
      <span className="wa-statusbar__metric" title="prompt tokens">
        prompt {prompt}
      </span>
      <span className="wa-statusbar__metric" title="completion tokens">
        completion {completion}
      </span>
      <span className="wa-statusbar__metric" title="total tokens">
        total {total}
        {estimated ? ' (估算)' : ''}
      </span>
      {pct !== null && (
        <span className="wa-statusbar__usage" title={`窗口占比 ${pct.toFixed(1)}%`}>
          <span className="wa-statusbar__bar">
            <span className="wa-statusbar__bar-fill" style={{ width: `${pct}%`, background: barColor }} />
          </span>
          {pct.toFixed(0)}%
        </span>
      )}
      <span className="wa-statusbar__path" title={projectRoot}>
        {projectRoot}
      </span>
      {sessionId && (
        <span className="wa-statusbar__sid" title={sessionId}>
          #{sessionId.slice(0, 8)}
        </span>
      )}
    </div>
  )
}
