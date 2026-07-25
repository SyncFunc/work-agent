// BackgroundAgents：展示后台子 agent 状态（运行中/完成）+ 实时事件流。
//
// M9 subsession：daemon 模式下，后台 subagent 走独立 subsession，其事件经父连接多路复用、
// 携带 subsession_id 实时到达。本组件按 subsession_id 分桶，复用主会话的 buildChatModel /
// MessageList 实时渲染子 agent 的文本/工具流（而非仅「已启动/已完成」两点）。
// CLI 模式无 subsession（子 agent 用本地 transport），仍由 notify 状态点兜底展示。

import { useEffect, useState } from 'react'
import { DaemonClient } from '../../protocol/client'
import { agentFromSubId, buildChatModel } from '../chat/useEventReducer'
import type { AgentEvent } from '../../protocol/types'
import { SubagentCard } from '../chat/SubagentCard'

interface BgTask {
  id: string
  agent: string
  status: 'running' | 'done'
  /** M9 subsession：该子会话累积的事件流（实时渲染用）。 */
  events: AgentEvent[]
}

interface Props {
  client: DaemonClient | null
}

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

    // M9 subsession：实时累积各子会话事件流。
    const offEv = client.onEvent((ev) => {
      const sub = ev.subsession_id
      if (typeof sub !== 'string' || !sub) return
      setTasks((prev) => {
        const existing = prev[sub]
        const events = existing ? [...existing.events, ev] : [ev]
        return upsert(prev, {
          id: sub,
          agent: agentFromSubId(sub),
          status: 'running',
          events,
        })
      })
    })

    // notify 状态点：保留旧行为（CLI 模式占位 / daemon 模式补全完成态）。
    const offMsg = client.onMessage('notify', (env) => {
      const msg = String((env.payload as { message?: string }).message ?? '')
      const done = msg.match(RE_DONE)
      if (done) {
        // 完成通知不含 subsession_id，按 agent 名把最近仍在运行的子会话标记完成（尽力）。
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
        // 仅当尚无该子会话实时事件时补占位（避免与 subsession 实时流重复）。
        setTasks((prev) => (prev[start[2]] ? prev : upsert(prev, {
          id: start[2],
          agent: start[1],
          status: 'running',
          events: [],
        })))
        return
      }
      const line = msg.match(RE_BG_LINE)
      if (line) {
        setTasks((prev) => (prev[line[1]] ? prev : upsert(prev, {
          id: line[1],
          agent: line[1],
          status: line[2].includes('已完成') ? 'done' : 'running',
          events: [],
        })))
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
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '4px 8px' }}>
        <span style={{ fontSize: 12, color: '#666' }}>后台子 Agent ({list.length})</span>
        <button type="button" onClick={refresh} style={{ fontSize: 12 }}>
          刷新 (/bg)
        </button>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', padding: '0 8px 8px', fontSize: 12 }}>
        {list.length === 0 ? (
          <p style={{ color: '#999' }}>暂无后台任务</p>
        ) : (
          list.map((t) => {
            // 同一任务的事件共享同一 subsession_id → buildChatModel 归约为单个 SubagentBlock。
            const model = buildChatModel(t.events)
            const sub = model.blocks[0]
            const subBlock = sub && sub.type === 'subagent' ? sub : null
            return (
              <div key={t.id}>
                {subBlock ? (
                  <SubagentCard block={subBlock} status={t.status} />
                ) : (
                  <div
                    style={{
                      padding: '6px 8px',
                      border: '1px solid #e0e0e0',
                      borderRadius: 6,
                      background: '#fafafa',
                      fontSize: 13,
                      color: '#888',
                    }}
                  >
                    <span style={{ fontFamily: 'monospace' }}>subagent:{t.agent || '(后台)'}</span>{' '}
                    · {t.status === 'running' ? '运行中' : '已完成'}（暂无实时流）
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
