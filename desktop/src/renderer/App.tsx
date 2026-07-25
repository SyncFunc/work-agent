import React, { useEffect, useRef, useState } from 'react'
import './theme.css'
import './layout.css'
import type { DaemonConfig } from '../shared/daemon-config'
import { DaemonClient } from '../protocol/client'
import { loadProjectRoot, saveProjectRoot } from '../features/projects/ProjectSwitcher'
import { SessionTabs } from '../features/sessions/SessionTabs'
import { SessionList } from '../features/sessions/SessionList'
import { useSessions } from '../features/sessions/useSessions'
import { MessageList } from '../features/chat/MessageList'
import { useChatModel } from '../features/chat/useEventReducer'
import { HitlModalHost } from '../features/hitl/HitlModalHost'
import { useHitl } from '../features/hitl/useHitl'
import { SettingsPanel } from '../features/settings/SettingsPanel'
import { applyTheme, loadSettings, loadTheme } from '../features/settings/settingsApi'
import { CommandPalette } from '../features/command/CommandPalette'
import { useCommands } from '../features/command/useCommands'
import { parseSlash } from '../features/command/parseSlash'
import { useNotices } from '../features/notices/useNotices'
import { ObsPanel } from '../features/obs/ObsPanel'
import { Button, IconButton, ToastStack } from '../components'
import type { ToastData, ToastKind } from '../components'
import { BarChart2, Bot, PanelLeft, Settings } from 'lucide-react'

const clamp = (v: number, lo: number, hi: number): number => Math.max(lo, Math.min(hi, v))

function loadNum(key: string, fallback: number): number {
  const raw = localStorage.getItem(key)
  const n = raw ? Number(raw) : NaN
  return Number.isFinite(n) ? clamp(n, 180, 460) : fallback
}

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
  const [sidebarW, setSidebarW] = useState(() => loadNum('workagent.sidebarW', 260))
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => localStorage.getItem('workagent.sidebarCollapsed') === '1')
  const [obsW, setObsW] = useState(() => loadNum('workagent.obsW', 340))
  const [isNarrow, setIsNarrow] = useState(() => (typeof window !== 'undefined' ? window.innerWidth < 900 : false))

  // 应用启动时套用持久化主题。
  useEffect(() => {
    applyTheme(loadTheme())
  }, [])

  // 拉取 daemon 配置并建连。
  useEffect(() => {
    let cancelled = false
    if (!window.agentApi) {
      setError(
        '未检测到客户端桥接（agentApi）。请通过 npm run dev 弹出的 Electron 窗口打开，' +
          '不要在普通浏览器中直接访问 localhost:5173。',
      )
      return
    }
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

  useEffect(() => {
    const off = window.agentApi.onProjectOpen?.((root: string) => {
      saveProjectRoot(root)
      setProjectRoot(root)
    })
    return () => off?.()
  }, [])

  // 窄窗检测：obs 面板转为抽屉。
  useEffect(() => {
    const onResize = (): void => setIsNarrow(window.innerWidth < 900)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // 拖拽改宽（侧栏 / obs）。
  const startResize = (which: 'sidebar' | 'obs') => (e: React.MouseEvent): void => {
    e.preventDefault()
    const startX = e.clientX
    const startW = which === 'sidebar' ? sidebarW : obsW
    const onMove = (ev: MouseEvent): void => {
      const delta = ev.clientX - startX
      const next =
        which === 'sidebar' ? clamp(startW + delta, 180, 460) : clamp(startW - delta, 260, 560)
      if (which === 'sidebar') {
        setSidebarW(next)
        localStorage.setItem('workagent.sidebarW', String(next))
      } else {
        setObsW(next)
        localStorage.setItem('workagent.obsW', String(next))
      }
    }
    const onUp = (): void => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }

  const sessions = useSessions(client, projectRoot)

  // 全局快捷键：Ctrl/Cmd+K 命令面板，+B 折叠侧栏，+J 切换可观测面板，+1..9 切会话。
  // tabs 随渲染变化，用 ref 持有最新值，避免闭包捕获过期引用。
  const tabsRef = useRef(sessions.state.tabs)
  tabsRef.current = sessions.state.tabs
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      const mod = e.ctrlKey || e.metaKey
      if (mod && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen((v) => !v)
      } else if (mod && e.key.toLowerCase() === 'b') {
        e.preventDefault()
        setSidebarCollapsed((v) => {
          const nv = !v
          localStorage.setItem('workagent.sidebarCollapsed', nv ? '1' : '0')
          return nv
        })
      } else if (mod && e.key.toLowerCase() === 'j') {
        e.preventDefault()
        setObsOpen((v) => !v)
      } else if (mod && /^[1-9]$/.test(e.key)) {
        e.preventDefault()
        const tab = tabsRef.current[Number(e.key) - 1]
        if (tab) sessions.switchSession(tab.id)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  const active = sessions.state.tabs.find((t) => t.id === sessions.state.activeId) ?? null
  const model = useChatModel(active ? active.events : [])
  const hitl = useHitl(client)
  const hitlPending = hitl.pending
  const commands = useCommands(client)
  const notices = useNotices(client)

  // 统一 toast 堆叠：通知（来自 daemon）与瞬时提示（如保存成功）共用一个右下角堆叠，避免重叠。
  const [savedToasts, setSavedToasts] = useState<ToastData[]>([])
  const dismissToast = (id: string): void => setSavedToasts((prev) => prev.filter((t) => t.id !== id))
  const pushToast = (kind: ToastKind, text: string): void => {
    const id = `t-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
    setSavedToasts((prev) => [...prev.slice(-3), { id, kind, text }])
  }
  const NOTICE_KIND: Record<string, ToastKind> = {
    notify: 'info',
    skills: 'info',
    agents: 'info',
    error: 'error',
  }
  const noticeToasts: ToastData[] = notices.map((n) => ({
    id: `n-${n.id}`,
    kind: NOTICE_KIND[n.kind] ?? 'info',
    text: n.text,
  }))

  const submit = (): void => {
    const text = draft.trim()
    if (!text) return
    const slash = parseSlash(text)
    if (slash && client) {
      client.command(slash.name, slash.args ? slash.args : null)
    } else {
      sessions.sendTask(text)
    }
    setDraft('')
  }

  const inputDisabled = !active || hitlPending

  return (
    <div className="wa-app">
      {/* 侧栏：可折叠 + 可拖拽伸缩 */}
      {sidebarCollapsed ? (
        <aside className="wa-sidebar wa-sidebar--collapsed">
          <div className="wa-brand" style={{ flexDirection: 'column', gap: 4 }}>
            <IconButton
              icon={<PanelLeft size={18} />}
              label="展开侧栏"
              onClick={() => {
                setSidebarCollapsed(false)
                localStorage.setItem('workagent.sidebarCollapsed', '0')
              }}
            />
            <IconButton icon={<BarChart2 size={18} />} label="可观测面板" onClick={() => setObsOpen((v) => !v)} />
            <IconButton icon={<Settings size={18} />} label="设置" onClick={() => setSettingsOpen(true)} />
          </div>
        </aside>
      ) : (
        <>
          <aside className="wa-sidebar" style={{ width: sidebarW }}>
            <div className="wa-brand">
              <h1 className="wa-brand__title">
                <Bot size={18} className="wa-brand__logo" />
                <span>Work Agent</span>
              </h1>
              <div className="wa-sidebar-actions">
                <IconButton icon={<BarChart2 size={18} />} label="可观测面板" onClick={() => setObsOpen((v) => !v)} />
                <IconButton icon={<Settings size={18} />} label="设置" onClick={() => setSettingsOpen(true)} />
                <IconButton
                  icon={<PanelLeft size={18} />}
                  label="折叠侧栏"
                  onClick={() => {
                    setSidebarCollapsed(true)
                    localStorage.setItem('workagent.sidebarCollapsed', '1')
                  }}
                />
              </div>
            </div>
            <div className="wa-project">
              <div className="wa-project__label">当前项目</div>
              <div className="wa-project__path" title={projectRoot}>
                {projectRoot || '—'}
              </div>
            </div>
            <div className="wa-sessionlist">
              <SessionList
                list={sessions.state.list}
                activeId={sessions.state.activeId}
                projectRoot={projectRoot}
                onOpen={sessions.openSession}
                onCreate={() => sessions.createSession()}
                onFork={sessions.forkSession}
              />
            </div>
          </aside>
          <div className="wa-resizer" onMouseDown={startResize('sidebar')} />
        </>
      )}

      {/* 主区：标签页 + 会话内容 + 贴底 composer */}
      <main className="wa-main">
        <SessionTabs
          tabs={sessions.state.tabs}
          activeId={sessions.state.activeId}
          onSwitch={sessions.switchSession}
          onClose={sessions.closeTab}
        />
        <section style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          {error && <p style={{ color: 'var(--wa-danger)', padding: '0 16px' }}>错误：{error}</p>}
          {!client && <p style={{ color: 'var(--wa-text-muted)', padding: '0 16px' }}>正在连接 daemon…</p>}
          {client && !active && <p style={{ color: 'var(--wa-text-muted)', padding: 16 }}>从左侧新建或打开一个会话开始。</p>}
          {active && (
            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
              <p style={{ color: 'var(--wa-text-muted)', fontSize: 13, padding: '0 16px', margin: '8px 0' }}>
                会话 <code>{active.name}</code> · {active.events.length} 条事件
                {sessions.state.replaying ? '（回放中…）' : ''}
              </p>
              <MessageList model={model} autoScroll />
            </div>
          )}
        </section>
        <footer className="wa-composer">
          <textarea
            className="wa-composer__input"
            value={draft}
            rows={1}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                submit()
              }
            }}
            placeholder={
              active
                ? hitlPending
                  ? '等待人工确认…'
                  : '输入任务，或 / 开头执行命令（Ctrl/Cmd+K 命令面板）'
                : '请先打开会话'
            }
            disabled={inputDisabled}
          />
          <Button variant="primary" onClick={submit} disabled={inputDisabled}>
            {parseSlash(draft) ? '执行' : '发送'}
          </Button>
        </footer>
      </main>

      {/* 可观测面板（drawer 或内联 + 拖拽） */}
      {obsOpen && (
        <ObsPanel
          client={client}
          projectRoot={projectRoot}
          sessionId={active ? active.id : null}
          contextWindow={contextWindow}
          onClose={() => setObsOpen(false)}
          drawer={isNarrow}
          width={obsW}
          onResizeStart={startResize('obs')}
        />
      )}

      {settingsOpen && (
        <SettingsPanel
          projectRoot={projectRoot}
          onClose={() => setSettingsOpen(false)}
          onToast={pushToast}
        />
      )}
      {paletteOpen && (
        <CommandPalette commands={commands.commands} onRun={commands.run} onClose={() => setPaletteOpen(false)} />
      )}
      <HitlModalHost
        requests={hitl.requests}
        onAnswer={hitl.resolveAsk}
        onConfirm={hitl.resolvePlan}
        onApprove={hitl.resolveApprove}
      />
      <ToastStack toasts={[...noticeToasts, ...savedToasts]} onDismiss={dismissToast} />
    </div>
  )
}
