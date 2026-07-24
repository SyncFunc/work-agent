import React, { useEffect, useState } from 'react'
import type { DaemonConfig } from '../shared/daemon-config'
import { DaemonClient } from '../protocol/client'
import { ProjectSwitcher, loadProjectRoot } from '../features/projects/ProjectSwitcher'
import { SessionTabs } from '../features/sessions/SessionTabs'
import { SessionList } from '../features/sessions/SessionList'
import { useSessions } from '../features/sessions/useSessions'

export default function App(): React.ReactElement {
  const [config, setConfig] = useState<DaemonConfig | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [client, setClient] = useState<DaemonClient | null>(null)
  const [projectRoot, setProjectRoot] = useState<string>('')
  const [draft, setDraft] = useState<string>('')

  // 拉取 daemon 配置并建连（DaemonClient 默认用全局 WebSocket 直连 daemon）。
  useEffect(() => {
    let cancelled = false
    window.agentApi
      .getDaemonConfig()
      .then((cfg) => {
        if (cancelled || !cfg) return
        setConfig(cfg)
        const c = new DaemonClient(cfg.wsUrl, { token: cfg.token })
        void c.connect().catch((e: unknown) => setError(String(e)))
        setClient(c)
      })
      .catch((e: unknown) => setError(String(e)))
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (config) setProjectRoot(loadProjectRoot(''))
  }, [config])

  const sessions = useSessions(client, projectRoot)
  const active = sessions.state.tabs.find((t) => t.id === sessions.state.activeId) ?? null

  const submit = (): void => {
    const text = draft.trim()
    if (!text) return
    sessions.sendTask(text)
    setDraft('')
  }

  return (
    <div style={{ display: 'flex', height: '100vh', fontFamily: 'system-ui, sans-serif' }}>
      {/* 侧栏：项目切换 + 会话列表 */}
      <aside style={{ width: 260, borderRight: '1px solid #eee', display: 'flex', flexDirection: 'column' }}>
        <h1 style={{ fontSize: 16, margin: 0, padding: '10px 12px', borderBottom: '1px solid #eee' }}>
          Work Agent
        </h1>
        <ProjectSwitcher projectRoot={projectRoot} onChange={setProjectRoot} />
        <SessionList
          list={sessions.state.list}
          activeId={sessions.state.activeId}
          projectRoot={projectRoot}
          onOpen={sessions.openSession}
          onCreate={() => sessions.createSession()}
          onFork={sessions.forkSession}
        />
      </aside>

      {/* 主区：标签页 + 会话内容（渲染在 M9.4） */}
      <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <SessionTabs
          tabs={sessions.state.tabs}
          activeId={sessions.state.activeId}
          onSwitch={sessions.switchSession}
          onClose={sessions.closeTab}
        />
        <section style={{ flex: 1, overflowY: 'auto', padding: 16 }}>
          {error && <p style={{ color: 'crimson' }}>错误：{error}</p>}
          {!client && <p>正在连接 daemon…</p>}
          {client && !active && <p style={{ color: '#888' }}>从左侧新建或打开一个会话开始。</p>}
          {active && (
            <div>
              <p style={{ color: '#888' }}>
                会话 <code>{active.name}</code> · 已加载 {active.events.length} 条事件
                {sessions.state.replaying ? '（回放中…）' : ''}
              </p>
              <p style={{ color: '#aaa', fontSize: 13 }}>
                （流式渲染将在 M9.4 实现；当前仅展示事件计数。）
              </p>
            </div>
          )}
        </section>
        <footer style={{ borderTop: '1px solid #eee', padding: 8, display: 'flex', gap: 8 }}>
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submit()
            }}
            placeholder={active ? '输入任务，回车发送…' : '请先打开会话'}
            disabled={!active}
            style={{ flex: 1 }}
          />
          <button onClick={submit} disabled={!active}>
            发送
          </button>
        </footer>
      </main>
    </div>
  )
}
