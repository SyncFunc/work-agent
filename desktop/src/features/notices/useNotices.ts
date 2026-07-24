// 订阅 daemon 的非阻塞反馈消息：notify / show_skills / show_agents。
// 维护一个自动消失的通知列表，供 NoticeHost 渲染。

import { useEffect, useState } from 'react'
import { DaemonClient } from '../../protocol/client'

export interface Notice {
  id: number
  kind: 'notify' | 'skills' | 'agents'
  text: string
}

let seq = 0

export function useNotices(client: DaemonClient | null, ttlMs = 6000): Notice[] {
  const [notices, setNotices] = useState<Notice[]>([])

  useEffect(() => {
    if (!client) return
    const push = (n: Notice): void => {
      setNotices((prev) => [...prev, n])
      setTimeout(() => {
        setNotices((prev) => prev.filter((x) => x.id !== n.id))
      }, ttlMs)
    }
    const offNotify = client.onMessage('notify', (env) => {
      push({ id: ++seq, kind: 'notify', text: String(env.payload['message'] ?? '') })
    })
    const offSkills = client.onMessage('show_skills', (env) => {
      const specs = (env.payload['specs'] as Array<Record<string, unknown>>) ?? []
      const names = specs.map((s) => String(s.name ?? s.id ?? '?')).join('、')
      push({ id: ++seq, kind: 'skills', text: names ? `可用人技能：${names}` : '无可用人技能' })
    })
    const offAgents = client.onMessage('show_agents', (env) => {
      const specs = (env.payload['specs'] as Array<Record<string, unknown>>) ?? []
      const names = specs.map((s) => String(s.name ?? s.id ?? '?')).join('、')
      push({ id: ++seq, kind: 'agents', text: names ? `可用子 Agent：${names}` : '无可用子 Agent' })
    })
    return () => {
      offNotify()
      offSkills()
      offAgents()
    }
  }, [client, ttlMs])

  return notices
}
