// 订阅 daemon 的非阻塞反馈消息：notify / show_skills / show_agents。
// 维护一个自动消失的通知列表，供 NoticeHost 渲染。

import { useEffect, useState } from 'react'
import { DaemonClient } from '../../protocol/client'

export interface Notice {
  id: number
  kind: 'notify' | 'skills' | 'agents' | 'error'
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
    // daemon 级错误信封（_route 顶层异常都会转成 error 发回，前端原本未监听 → 静默丢失）。
    // 接入后「新建会话无反应」等异常会变成可见提示，便于定位根因。
    const offError = client.onMessage('error', (env) => {
      const code = env.payload['code']
      const message = env.payload['message']
      const text = `daemon 错误${code ? `（${code}）` : ''}：${message ?? '未知错误'}`
      push({ id: ++seq, kind: 'error', text })
    })
    return () => {
      offNotify()
      offSkills()
      offAgents()
      offError()
    }
  }, [client, ttlMs])

  return notices
}
