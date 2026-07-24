// BackgroundAgents：展示后台子 agent 状态（运行中/完成）。
// 数据源：notify 中的后台 Subagent 启动/完成通知 + 手动 /bg 刷新。

import { useEffect, useState } from 'react'
import { DaemonClient } from '../../protocol/client'

interface BgTask {
  id: string
  agent: string
  status: 'running' | 'done'
}

interface Props {
  client: DaemonClient | null
}

const RE_START = /后台 Subagent \[(.+?)\] 已启动（task_id: (.+?)）/
const RE_DONE = /后台 Subagent \[(.+?)\] 已完成/
const RE_BG_LINE = /^\s*(bg_[0-9a-f]+):\s*(✅ 已完成|🔄 运行中)/

function upsert(tasks: BgTask[], t: BgTask): BgTask[] {
  const idx = tasks.findIndex((x) => x.id === t.id)
  if (idx < 0) return [...tasks, t]
  const next = tasks.slice()
  next[idx] = { ...next[idx], ...t }
  return next
}

export function BackgroundAgents({ client }: Props) {
  const [tasks, setTasks] = useState<BgTask[]>([])

  useEffect(() => {
    if (!client) return
    const off = client.onMessage('notify', (env) => {
      const msg = String((env.payload as { message?: string }).message ?? '')
      const start = msg.match(RE_START)
      if (start) {
        setTasks((prev) => upsert(prev, { id: start[2], agent: start[1], status: 'running' }))
        return
      }
      const done = msg.match(RE_DONE)
      if (done) {
        // 完成通知不含 task_id，按 agent 名把最近仍在运行的任务标记完成（尽力而为）。
        setTasks((prev) =>
          prev.map((t) => (t.agent === done[1] && t.status === 'running' ? { ...t, status: 'done' } : t)),
        )
        return
      }
      const line = msg.match(RE_BG_LINE)
      if (line) {
        setTasks((prev) =>
          upsert(prev, { id: line[1], status: line[2].includes('已完成') ? 'done' : 'running' }),
        )
      }
    })
    return off
  }, [client])

  const refresh = (): void => {
    client?.command('bg')
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '4px 8px' }}>
        <span style={{ fontSize: 12, color: '#666' }}>后台子 Agent ({tasks.length})</span>
        <button type="button" onClick={refresh} style={{ fontSize: 12 }}>
          刷新 (/bg)
        </button>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', padding: '0 8px 8px', fontSize: 12 }}>
        {tasks.length === 0 ? (
          <p style={{ color: '#999' }}>暂无后台任务</p>
        ) : (
          tasks.map((t) => (
            <div
              key={t.id}
              style={{
                display: 'flex',
                gap: 6,
                alignItems: 'center',
                padding: '3px 0',
                borderBottom: '1px solid #f0f0f0',
              }}
            >
              <span style={{ fontSize: 14 }}>{t.status === 'running' ? '🔄' : '✅'}</span>
              <span style={{ fontWeight: 600 }}>{t.agent || '(后台)'}</span>
              <span style={{ color: '#999' }}>{t.id}</span>
              <span style={{ marginLeft: 'auto', color: t.status === 'running' ? '#1a73e8' : '#1e7e34' }}>
                {t.status === 'running' ? '运行中' : '已完成'}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
