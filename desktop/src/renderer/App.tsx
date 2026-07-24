import React, { useEffect, useState } from 'react'
import './theme.css'
import type { DaemonConfig } from '../shared/daemon-config'
import { DaemonClient } from '../protocol/client'
import { ProjectSwitcher, loadProjectRoot } from '../features/projects/ProjectSwitcher'
import { SessionTabs } from '../features/sessions/SessionTabs'
import { SessionList } from '../features/sessions/SessionList'
import { useSessions } from '../features/sessions/useSessions'
import { MessageList } from '../features/chat/MessageList'
import { useChatModel } from '../features/chat/useEventReducer'
import { HitlModalHost } from '../features/hitl/HitlModalHost'
import { useHitl } from '../features/hitl/useHitl'
import { SettingsPanel } from '../features/settings/SettingsPanel'
import { applyTheme, loadTheme } from '../features/settings/settingsApi'
import { CommandPalette } from '../features/command/CommandPalette'
import { useCommands } from '../features/command/useCommands'
import { parseSlash } from '../features/command/parseSlash'
import { NoticeHost } from '../features/notices/NoticeHost'
import { useNotices } from '../features/notices/useNotices'
import { ObsPanel } from '../features/obs/ObsPanel'
import { loadSettings } from '../features/settings/settingsApi'

export default function App(): React.ReactElement {
  const [config, setConfig] = useState<DaemonConfig | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [client, setClient] = useState<DaemonClient | null>(null)
  const [projectRoot, setProjectRoot] = useState<string>('')
  const [draft, setDraft] = useState<string>('')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [obsOpen, setObsOpen] = useState(false)
  const [contextWindow, setContextWindow] = useState<number | undefined>(undefined)

  // 应用启动时套用持久化主题。
  useEffect(() => {
    applyTheme(loadTheme())
  }, [])

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

  // 拉取项目 settings 的上下文窗口大小，供状态栏占比展示。
  useEffect(() => {
    if (!projectRoot) return
    let cancelled = false
    loadSettings(projectRoot)
      .then((s) => {
        if (!cancelled) setContextWindow(s.context?.context_window)
      })
      .catch(() => {
        if (!cancelled) setContextWindow(undefined)
      })
    return () => {
      cancelled = true
    }
  }, [projectRoot])

  // 全局快捷键：Ctrl/Cmd+K 打开命令面板。
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen((v) => !v)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const sessions = useSessions(client, projectRoot)
  const active = sessions.state.tabs.find((t) => t.id === sessions.state.activeId) ?? null
  const model = useChatModel(active ? active.events : [])
  const hitl = useHitl(client)
  const hitlPending = hitl.pending
  const commands = useCommands(client)
  const notices = useNotices(client)

  const submit = (): void => {
    const text = draft.trim()
    if (!text) return
    const slash = parseSlash(text)
    if (slash && client) {
      // 斜杠命令：直接发 command（与 M7.3 CLI REPL 一致）。
      client.command(slash.name, slash.args ? slash.args : null)
    } else {
      sessions.sendTask(text)
    }
    setDraft('')
  }

  return (
    <div style={{ display: 'flex', height: '100vh', fontFamily: 'system-ui, sans-serif' }}>
      {/* 侧栏：项目切换 + 会话列表 */}
      <aside style={{ width: 260, borderRight: '1px solid #eee', display: 'flex', flexDirection: 'column' }}>
        <h1 style={{ fontSize: 16, margin: 0, padding: '10px 12px', borderBottom: '1px solid #eee', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span>Work Agent</span>
          <span style={{ display: 'flex', gap: 8 }}>
            <button type="button" onClick={() => setObsOpen((v) => !v)} title="可观测面板" style={{ fontSize: 14 }}>📊</button>
            <button type="button" onClick={() => setSettingsOpen(true)} title="设置" style={{ fontSize: 14 }}>⚙</button>
          </span>
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
              <p style={{ color: '#888', fontSize: 13 }}>
                会话 <code>{active.name}</code> · {active.events.length} 条事件
                {sessions.state.replaying ? '（回放中…）' : ''}
              </p>
              <MessageList model={model} />
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
            placeholder={
              active
                ? hitlPending
                  ? '等待人工确认…'
                  : '输入任务，或 / 开头执行命令（Ctrl/Cmd+K 命令面板）'
                : '请先打开会话'
            }
            disabled={!active || hitlPending}
            style={{ flex: 1 }}
          />
          <button onClick={submit} disabled={!active || hitlPending}>
            {parseSlash(draft) ? '执行' : '发送'}
          </button>
        </footer>
      </main>
      {obsOpen && (
        <ObsPanel
          client={client}
          projectRoot={projectRoot}
          sessionId={active ? active.id : null}
          contextWindow={contextWindow}
          onClose={() => setObsOpen(false)}
        />
      )}
      {settingsOpen && <SettingsPanel projectRoot={projectRoot} onClose={() => setSettingsOpen(false)} />}
      {paletteOpen && (
        <CommandPalette
          commands={commands.commands}
          onRun={commands.run}
          onClose={() => setPaletteOpen(false)}
        />
      )}
      <HitlModalHost
        requests={hitl.requests}
        onAnswer={hitl.resolveAsk}
        onConfirm={hitl.resolvePlan}
        onApprove={hitl.resolveApprove}
      />
      <NoticeHost notices={notices} />
    </div>
  )
}
