// BackgroundAgents：展示后台子 agent 状态（运行中/完成）+ 实时事件流。

import { useEffect, useState } from 'react'
import { DaemonClient } from '../../protocol/client'
import { agentFromSubId, buildChatModel } from '../chat/useEventReducer'
import type { AgentEvent } from '../../protocol/types'
import { SubagentCard } from '../chat/SubagentCard'
import { IconButton } from '../../components'
import { RefreshCw } from 'lucide-react'

interface BgTask {
  id: string
  agent: string
  status: 'running' | 'done'
  events: AgentEvent[]
}

interface Props {
  client: DaemonClient | null
}

// 注：正则匹配 daemon 下发的状态点文本（含 emoji），属数据匹配，非 UI 图标。
const RE_START = /后台 Subagent \[(.+?)\] 已启动（task_id: (.+?)）/
const RE_DONE = /后台 Subagent \[(.+?)\] 已完成/
const RE_BG_LINE = /^\s*(bg_[0-9a-f]+):\s*(✅ 已完成|🔄 运行中)/

function upsert(tasks: Record<string, BgTask>, t: BgTask): Record<string, BgTask> {
  return { ...tasks, [t.id]: t }
}

export function BackgroundAgents({ client }: Props) {
  const [tasks, setTasks] = useState<Record<string, BgTask>>({})

  useEffect(() => {
    if (!client) return

    const offEv = client.onEvent((ev) => {
      const sub = ev.subsession_id
      if (typeof sub !== 'string' || !sub) return
      setTasks((prev) => {
        const existing = prev[sub]
        const events = existing ? [...existing.events, ev] : [ev]
        return upsert(prev, { id: sub, agent: agentFromSubId(sub), status: 'running', events })
      })
    })

    const offMsg = client.onMessage('notify', (env) => {
      const msg = String((env.payload as { message?: string }).message ?? '')
      const done = msg.match(RE_DONE)
      if (done) {
        setTasks((prev) => {
          const next = { ...prev }
          for (const k of Object.keys(next)) {
            const t = next[k]
            if (t.agent === done[1] && t.status === 'running') next[k] = { ...t, status: 'done' }
          }
          return next
        })
        return
      }
      const start = msg.match(RE_START)
      if (start) {
        setTasks((prev) => (prev[start[2]] ? prev : upsert(prev, { id: start[2], agent: start[1], status: 'running', events: [] })))
        return
      }
      const line = msg.match(RE_BG_LINE)
      if (line) {
        setTasks((prev) => (prev[line[1]] ? prev : upsert(prev, { id: line[1], agent: line[1], status: line[2].includes('已完成') ? 'done' : 'running', events: [] })))
      }
    })

    return () => {
      offEv()
      offMsg()
    }
  }, [client])

  const refresh = (): void => {
    client?.command('bg')
  }

  const list = Object.values(tasks)

  return (
    <div className="wa-bg">
      <div className="wa-bg__head">
        <span className="wa-bg__count">后台子 Agent ({list.length})</span>
        <IconButton icon={<RefreshCw size={14} />} label="刷新后台任务" onClick={refresh} />
      </div>
      <div className="wa-bg__list">
        {list.length === 0 ? (
          <p className="wa-bg__empty">暂无后台任务</p>
        ) : (
          list.map((t) => {
            const model = buildChatModel(t.events)
            const sub = model.blocks[0]
            const subBlock = sub && sub.type === 'subagent' ? sub : null
            return (
              <div key={t.id}>
                {subBlock ? (
                  <SubagentCard block={subBlock} status={t.status} />
                ) : (
                  <div className="wa-bg__placeholder">
                    <span className="wa-subagent__title">subagent:{t.agent || '(后台)'}</span> ·{' '}
                    {t.status === 'running' ? '运行中' : '已完成'}（暂无实时流）
                  </div>
                )}
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
