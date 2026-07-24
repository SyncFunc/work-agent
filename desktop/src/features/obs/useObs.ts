// useObs：订阅 daemon 的 usage / notify / plan 信号，汇聚为可观测状态（token 用量、模式、日志）。
// 纯订阅 + useState，无副作用逻辑，便于在组件内直接使用。

import { useEffect, useState } from 'react'
import { DaemonClient } from '../../protocol/client'
import type { UsagePayload } from '../../protocol/types'

export type ObsMode = 'plan' | 'exec'

export interface ObsLog {
  id: number
  ts: number
  message: string
}

export interface UseObs {
  usage: UsagePayload['usage'] | null
  estimated: boolean
  mode: ObsMode
  logs: ObsLog[]
  clearLogs: () => void
}

let logSeq = 0

export function useObs(client: DaemonClient | null): UseObs {
  const [usage, setUsage] = useState<UsagePayload['usage'] | null>(null)
  const [estimated, setEstimated] = useState(false)
  const [mode, setMode] = useState<ObsMode>('exec')
  const [logs, setLogs] = useState<ObsLog[]>([])

  useEffect(() => {
    if (!client) return
    const offUsage = client.onMessage('usage', (env) => {
      const p = env.payload as UsagePayload
      setUsage(p.usage ?? null)
      setEstimated(Boolean(p.estimated))
    })
    const offNotify = client.onMessage('notify', (env) => {
      const p = env.payload as { message?: string }
      setLogs((prev) => [...prev, { id: ++logSeq, ts: Date.now(), message: String(p.message ?? '') }])
    })
    // 模式推断（启发式）：收到 plan 相关协议 → plan；收到带工具调用的决策 → exec。
    const offShowPlan = client.onMessage('show_plan', () => setMode('plan'))
    const offConfirmPlan = client.onMessage('confirm_plan', () => setMode('plan'))
    const offEvent = client.onEvent((ev) => {
      if (ev.type === 'decision' && ev.decision && ev.decision.tool_calls.length > 0) {
        setMode('exec')
      }
    })
    return () => {
      offUsage()
      offNotify()
      offShowPlan()
      offConfirmPlan()
      offEvent()
    }
  }, [client])

  const clearLogs = (): void => setLogs([])
  return { usage, estimated, mode, logs, clearLogs }
}
