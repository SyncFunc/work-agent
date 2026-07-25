// BackgroundAgents：展示当前会话的后台子 agent 状态（运行中/已完成/出错）。
//
// 关键约束（修复「前台子 agent 也出现在后台、跨会话串台、切回后消失」等 bug）：
//   1. 只追踪「后台」子 agent —— 数据来源是 daemon 下发的 notify 文本
//      （RE_START / RE_DONE / RE_ERROR / RE_BG_LINE），前台子 agent 走主聊天区
//      的 event 流，不会下发这些 notify，因此不会误入后台面板。
//   2. 只展示「当前会话」的后台任务 —— daemon 仅向当前 attach 的会话连接转发 notify；
//      本组件在 sessionId 变化时清空任务表并重新拉取，避免跨会话串台与切回后残留/消失。

import { useEffect, useState } from 'react'
import { DaemonClient } from '../../protocol/client'
import { Badge, IconButton } from '../../components'
import { Bot, CheckCircle2, Loader, RefreshCw, XCircle } from 'lucide-react'

interface BgTask {
  id: string
  agent: string
  status: 'running' | 'done' | 'error'
}

interface Props {
  client: DaemonClient | null
  /** 当前 attach 的会话 id；变化即清空并按新会话重新拉取后台任务。 */
  sessionId: string | null
}

// 注：以下正则匹配 daemon 下发的状态点文本（含 emoji），属数据匹配，非 UI 图标。
const RE_START = /后台 Subagent \[(.+?)\] 已启动（task_id: (.+?)）/
const RE_DONE = /后台 Subagent \[(.+?)\] 已完成/
const RE_ERROR = /后台 Subagent \[(.+?)\] 出错/
const RE_BG_LINE = /^\s*(bg_[0-9a-f]+):\s*(✅ 已完成|🔄 运行中)/

function upsert(tasks: Record<string, BgTask>, t: BgTask): Record<string, BgTask> {
  return { ...tasks, [t.id]: t }
}

export function BackgroundAgents({ client, sessionId }: Props) {
  const [tasks, setTasks] = useState<Record<string, BgTask>>({})

  // 会话切换：清空旧会话的后台任务，并拉取新会话当前正在运行的后台任务。
  // client.send 与 session.switch 同 FIFO，daemon 先处理 switch 再处理本 bg 查询，
  // 因此返回的是新会话的任务，不会串台。
  useEffect(() => {
    setTasks({})
    client?.command('bg')
  }, [client, sessionId])

  useEffect(() => {
    if (!client) return

    const offMsg = client.onMessage('notify', (env) => {
      const msg = String((env.payload as { message?: string }).message ?? '')

      // 完成（成功）：按 agent 名把该后台任务置为 done。
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

      // 出错：按 agent 名置为 error。
      const err = msg.match(RE_ERROR)
      if (err) {
        setTasks((prev) => {
          const next = { ...prev }
          for (const k of Object.keys(next)) {
            const t = next[k]
            if (t.agent === err[1] && t.status === 'running') next[k] = { ...t, status: 'error' }
          }
          return next
        })
        return
      }

      // 启动：登记一个新后台任务（task_id 即 bg_xxx）。
      const start = msg.match(RE_START)
      if (start) {
        setTasks((prev) =>
          prev[start[2]] ? prev : upsert(prev, { id: start[2], agent: start[1], status: 'running' }),
        )
        return
      }

      // /bg 状态行：补全/纠正运行中任务的状态（已完成的不在 _bg_tasks 中，故显式置 done）。
      const line = msg.match(RE_BG_LINE)
      if (line) {
        const id = line[1]
        const status = line[2].includes('已完成') ? 'done' : 'running'
        setTasks((prev) => upsert(prev, { id, agent: prev[id]?.agent ?? id, status }))
      }
    })

    return () => offMsg()
  }, [client])

  const refresh = (): void => {
    client?.command('bg')
  }

  const list = Object.values(tasks)

  const statusBadge = (status: BgTask['status']): React.ReactNode => {
    if (status === 'running') {
      return (
        <Badge tone="primary" icon={<Loader size={12} />}>
          运行中
        </Badge>
      )
    }
    if (status === 'done') {
      return (
        <Badge tone="success" icon={<CheckCircle2 size={12} />}>
          已完成
        </Badge>
      )
    }
    return (
      <Badge tone="danger" icon={<XCircle size={12} />}>
        出错
      </Badge>
    )
  }

  return (
    <div className="wa-bg">
      <div className="wa-bg__head">
        <span className="wa-bg__count">后台子 Agent ({list.length})</span>
        <IconButton icon={<RefreshCw size={14} />} label="刷新后台任务" onClick={refresh} />
      </div>
      <div className="wa-bg__list">
        {list.length === 0 ? (
          <p className="wa-bg__empty">本会话暂无后台任务</p>
        ) : (
          list.map((t) => (
            <div key={t.id} className="wa-bg__item">
              <Bot size={15} />
              <span className="wa-bg__name">{t.agent}</span>
              {statusBadge(t.status)}
              <span className="wa-bg__id" title={t.id}>
                {t.id}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
