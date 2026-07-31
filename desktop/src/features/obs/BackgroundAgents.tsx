// BackgroundAgents：展示当前会话的后台子 agent 状态（仅运行中）。
//
// M11：数据来源改为「后台 subsession 事件流」—— daemon 已将后台子 agent（如
// session-memory）的 subsession 事件（background=true）实时转发到事件流。
// 本组件监听事件流：后台 subsession 首个事件 → 登记运行中；收到该 subsession 的
// final/error → 移除。切换会话时清空任务表，按新会话事件流重新登记。
// 相比旧的 notify 文本追踪，后台子 agent 无需额外下发 notify 即可感知启动/完成。

import { useEffect, useRef, useState } from 'react'
import { DaemonClient } from '../../protocol/client'
import { Badge, IconButton } from '../../components'
import { Bot, Loader, RefreshCw } from 'lucide-react'
import { agentFromSubId } from '../chat/useEventReducer'

interface BgTask {
  id: string
  agent: string
  status: 'running' | 'done' | 'error'
}

interface Props {
  client: DaemonClient | null
  /** 当前 attach 的会话 id；变化即清空并基于新会话事件流重新登记。 */
  sessionId: string | null
  /** 后台任务状态变化回调（空→有运行中 / 有→空），用于驱动入口按钮呼吸动画。 */
  onRunningChange?: (running: boolean) => void
}

/** 仅追踪当前会话的后台 subsession（排除顶层会话事件）。 */
function isBackgroundEvent(ev: { background?: boolean; subsession_id?: string | null }): boolean {
  return ev.background === true && typeof ev.subsession_id === 'string'
}

export function BackgroundAgents({ client, sessionId, onRunningChange }: Props) {
  const [tasks, setTasks] = useState<Record<string, BgTask>>({})
  // 记录已收到 final/error 的后台 subsession，避免回放时误登记已完成的旧任务。
  const finishedRef = useRef<Set<string>>(new Set())
  // 持有最新的 onRunningChange 回调，避免事件回调闭包捕获过期引用。
  const onRunningRef = useRef(onRunningChange)
  onRunningRef.current = onRunningChange

  // 任务表变化时向父级汇报「当前是否有后台任务在运行」，驱动入口按钮呼吸动画。
  useEffect(() => {
    onRunningRef.current?.(Object.keys(tasks).length > 0)
  }, [tasks])

  // 会话切换：清空旧会话任务，重置已结束标记，按新会话事件流重新登记运行中的后台任务。
  useEffect(() => {
    setTasks({})
    finishedRef.current = new Set()
  }, [client, sessionId])

  useEffect(() => {
    if (!client) return

    const offEvent = client.onEvent((ev) => {
      if (!isBackgroundEvent(ev)) return
      const sub = ev.subsession_id as string

      // 结束事件：从任务表移除（仅展示运行中），并记录该 subsession 已结束
      if (ev.type === 'final' || ev.type === 'error') {
        finishedRef.current.add(sub)
        setTasks((prev) => {
          if (!(sub in prev)) return prev
          const next = { ...prev }
          delete next[sub]
          return next
        })
        return
      }

      // 运行中事件：若该 subsession 未结束且尚未登记，则登记为运行中
      setTasks((prev) => {
        if (finishedRef.current.has(sub) || prev[sub]) return prev
        return { ...prev, [sub]: { id: sub, agent: agentFromSubId(sub), status: 'running' } }
      })
    })

    return () => offEvent()
  }, [client])

  const refresh = (): void => {
    // 事件流兜底刷新：清空并重置，等待新事件流重新登记（避免误清已结束记录）。
    setTasks({})
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
          <p className="wa-bg__empty">本会话暂无运行中的后台任务</p>
        ) : (
          list.map((t) => (
            <div key={t.id} className="wa-bg__item">
              <span className="wa-bg__item-icon">
                <Bot size={15} />
              </span>
              <div className="wa-bg__main">
                <div className="wa-bg__row">
                  <span className="wa-bg__name">{t.agent}</span>
                  <Badge tone="primary" icon={<Loader size={12} />}>
                    运行中
                  </Badge>
                </div>
                <div className="wa-bg__meta">
                  <span className="wa-bg__id" title={t.id}>
                    {t.id}
                  </span>
                </div>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
