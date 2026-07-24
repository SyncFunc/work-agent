// useSessions：连接 DaemonClient 与纯 reducer（sessionMachine）+ ReplayBuffer。
// 所有 daemon 消息在此汇聚为 React 状态；replay 期间事件进 ReplayBuffer，结束整体回填。

import { useCallback, useEffect, useReducer, useRef } from 'react'
import { DaemonClient } from '../../protocol/client'
import type { AgentEvent, SessionInfo } from '../../protocol/types'
import { ReplayBuffer } from './replay'
import { initialState, sessionsReducer } from './sessionMachine'

export interface UseSessions {
  state: ReturnType<typeof sessionsReducer>
  createSession: (name?: string) => void
  openSession: (id: string) => void
  switchSession: (id: string) => void
  closeTab: (id: string) => void
  forkSession: (id: string) => void
  sendTask: (text: string, opts?: { yes?: boolean; plan?: boolean }) => void
}

export function useSessions(client: DaemonClient | null, projectRoot: string): UseSessions {
  const [state, dispatch] = useReducer(sessionsReducer, projectRoot, initialState)
  const replay = useRef(new ReplayBuffer())
  const prevListIds = useRef<Set<string> | null>(null)
  const forkPending = useRef(false)

  useEffect(() => {
    if (!client) return
    const offWelcome = client.onMessage('welcome', () => {
      client.listSessions(projectRoot)
    })
    const offList = client.onMessage('session_list', (env) => {
      const list = (env.payload['sessions'] as SessionInfo[] | undefined) ?? []
      dispatch({ type: 'sessionList', list })
      if (forkPending.current) {
        forkPending.current = false
        const before = prevListIds.current
        const fresh = list.find((s) => !before || !before.has(s.id))
        if (fresh) client.attach(fresh.id, projectRoot)
      }
      prevListIds.current = new Set(list.map((s) => s.id))
    })
    const offCreated = client.onMessage('session.created', (env) => {
      const id = env.payload['session_id'] as string
      const name = (env.payload['name'] as string | null) ?? null
      const pr = (env.payload['project_root'] as string | undefined) ?? projectRoot
      dispatch({ type: 'sessionCreated', id, name, projectRoot: pr })
    })
    const offAttached = client.onMessage('attached', (env) => {
      const id = env.payload['session_id'] as string
      const pr = (env.payload['project_root'] as string | undefined) ?? projectRoot
      dispatch({ type: 'attached', id, projectRoot: pr })
    })
    const offReplayStart = client.onMessage('replay_start', () => {
      replay.current.start()
      dispatch({ type: 'replayStart' })
    })
    const offReplayEnd = client.onMessage('replay_end', () => {
      const events = replay.current.end()
      dispatch({ type: 'replayEnd', events })
    })
    const offEvent = client.onEvent((ev: AgentEvent) => {
      if (replay.current.isActive) dispatch({ type: 'replayEvent', event: ev })
      else dispatch({ type: 'liveEvent', event: ev })
    })
    return () => {
      offWelcome()
      offList()
      offCreated()
      offAttached()
      offReplayStart()
      offReplayEnd()
      offEvent()
    }
  }, [client, projectRoot])

  // 项目根变化时重置列表并刷新（同一 daemon 不重启）。
  useEffect(() => {
    dispatch({ type: 'setProjectRoot', projectRoot })
    client?.listSessions(projectRoot)
  }, [client, projectRoot])

  const createSession = useCallback(
    (name?: string) => {
      client?.newSession(name, projectRoot)
    },
    [client, projectRoot],
  )

  const openSession = useCallback(
    (id: string) => {
      client?.attach(id, projectRoot)
    },
    [client, projectRoot],
  )

  const switchSession = useCallback(
    (id: string) => {
      client?.switch(id, projectRoot)
    },
    [client, projectRoot],
  )

  const closeTab = useCallback(
    (id: string) => {
      if (state.activeId === id) client?.detach()
      dispatch({ type: 'closeTab', id })
    },
    [client, state.activeId],
  )

  const forkSession = useCallback(
    (id: string) => {
      if (!client) return
      forkPending.current = true
      prevListIds.current = new Set(state.list.map((s) => s.id))
      client.command('fork', id)
      client.listSessions(projectRoot)
    },
    [client, projectRoot, state.list],
  )

  const sendTask = useCallback(
    (text: string, opts?: { yes?: boolean; plan?: boolean }) => {
      client?.sendTask(text, { ...opts, projectRoot })
    },
    [client, projectRoot],
  )

  return { state, createSession, openSession, switchSession, closeTab, forkSession, sendTask }
}
