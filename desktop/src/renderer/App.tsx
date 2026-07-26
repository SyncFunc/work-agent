import React, { useCallback, useEffect, useRef, useState } from 'react'
import './theme.css'
import './layout.css'
import type { DaemonConfig } from '../shared/daemon-config'
import { DaemonClient } from '../protocol/client'
import { loadProjectRoot, saveProjectRoot } from '../features/projects/ProjectSwitcher'
import { SessionTabs } from '../features/sessions/SessionTabs'
import { useSessions } from '../features/sessions/useSessions'
import { MessageList } from '../features/chat/MessageList'
import { Composer } from '../features/chat/Composer'
import type { TurnMeta, UsageSummary } from '../features/chat/MessageItem'
import { useChatModel } from '../features/chat/useEventReducer'
import { HitlModalHost } from '../features/hitl/HitlModalHost'
import { useHitl } from '../features/hitl/useHitl'
import { SettingsPanel } from '../features/settings/SettingsPanel'
import { applyTheme, loadSettings, loadTheme } from '../features/settings/settingsApi'
import { CommandPalette } from '../features/command/CommandPalette'
import { useCommands } from '../features/command/useCommands'
import { useSkills } from '../features/command/useSkills'
import { parseSlash } from '../features/command/parseSlash'
import { useNotices } from '../features/notices/useNotices'
import { ObsPanel } from '../features/obs/ObsPanel'
import { useObs } from '../features/obs/useObs'
import { Sidebar } from '../features/sidebar/Sidebar'
import type { LeftNav } from '../features/sidebar/Sidebar'
import { SkillsPanel, AgentsPanel } from '../features/sidebar/SpecPanels'
import { ToastStack, TitleBar } from '../components'
import type { ToastData, ToastKind } from '../components'
import { SplashScreen } from '../components/SplashScreen'
import type { SplashStep } from '../components/SplashScreen'
import type { DaemonStage } from '../shared/daemon-config'

const APP_NAME = 'Work Agent'
const APP_VERSION = '0.9'

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
  // 启动遮罩状态：后台启动阶段、WebSocket 是否已连上、遮罩是否仍可见（连上后淡出）。
  const [stage, setStage] = useState<DaemonStage>('spawning')
  const [wsConnected, setWsConnected] = useState(false)
  const [splashVisible, setSplashVisible] = useState(true)
  const [projectRoot, setProjectRoot] = useState<string>('')
  const [draft, setDraft] = useState<string>('')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [leftNav, setLeftNav] = useState<LeftNav>('chat')
  const [contextWindow, setContextWindow] = useState<number | undefined>(undefined)
  const [sidebarW, setSidebarW] = useState(() => loadNum('workagent.sidebarW', 260))
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => localStorage.getItem('workagent.sidebarCollapsed') === '1')
  // M9.9 步骤4：会话级 plan_mode / model（来自 SESSION_INFO），与生成中状态（running）。
  const [sessionInfo, setSessionInfo] = useState<{ planMode: boolean; model: string }>({ planMode: false, model: '' })
  const [running, setRunning] = useState(false)
  // M9.9 步骤7：单轮 Token 明细 / 耗时，挂到会话最后一条助手文本块。
  const [turnMeta, setTurnMeta] = useState<TurnMeta | null>(null)
  const turnStartRef = useRef<number>(0)
  const turnUsageRef = useRef<UsageSummary>(emptyUsage())
  const estimatedRef = useRef<boolean>(false)
  const runningRef = useRef<boolean>(false)
  useEffect(() => {
    runningRef.current = running
  }, [running])
  const finishTurn = useCallback(() => {
    setRunning(false)
    setTurnMeta({ duration: (Date.now() - turnStartRef.current) / 1000, usage: { ...turnUsageRef.current } })
  }, [])

  function emptyUsage(): UsageSummary {
    return {
      prompt_tokens: 0,
      completion_tokens: 0,
      reasoning_tokens: 0,
      cache_hit_tokens: 0,
      cache_miss_tokens: 0,
      cache_write_tokens: 0,
      total_tokens: 0,
    }
  }
  function addUsage(acc: UsageSummary, u: Partial<UsageSummary>): void {
    acc.prompt_tokens += u.prompt_tokens ?? 0
    acc.completion_tokens += u.completion_tokens ?? 0
    acc.reasoning_tokens += u.reasoning_tokens ?? 0
    acc.cache_hit_tokens += u.cache_hit_tokens ?? 0
    acc.cache_miss_tokens += u.cache_miss_tokens ?? 0
    acc.cache_write_tokens += u.cache_write_tokens ?? 0
    acc.total_tokens += u.total_tokens ?? 0
  }

  // 应用启动时套用持久化主题。
  useEffect(() => {
    applyTheme(loadTheme())
  }, [])

  // 启动遮罩：React 挂载后移除静态 #splash，改由 SplashScreen 接管（无缝衔接，无白屏）。
  // 订阅主进程推送的后台启动阶段，驱动连接进度展示。
  useEffect(() => {
    document.getElementById('splash')?.remove()
    const api = window.agentApi
    if (api?.getDaemonStage) {
      void api
        .getDaemonStage()
        .then((s) => {
          if (s) setStage(s)
        })
        .catch(() => {})
    }
    const off = api?.onDaemonProgress?.((s) => setStage(s))
    return () => off?.()
  }, [])

  // 拉取 daemon 配置并建连（轮询：daemon 未就绪时窗口已可见，显示「正在连接…」而非白屏）。
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const tryConfig = (): void => {
      if (cancelled) return
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
          if (cancelled) return
          if (cfg) {
            setConfig(cfg)
            const c = new DaemonClient(cfg.wsUrl, { token: cfg.token })
            void c
              .connect()
              .then(() => setWsConnected(true))
              .catch((e: unknown) => setError(String(e)))
            setClient(c)
          } else {
            timer = setTimeout(tryConfig, 400)
          }
        })
        .catch(() => {
          if (!cancelled) timer = setTimeout(tryConfig, 400)
        })
    }
    tryConfig()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [])

  // WebSocket 连上后，延迟淡出遮罩，露出工作台。
  useEffect(() => {
    if (!wsConnected) return
    const t = setTimeout(() => setSplashVisible(false), 450)
    return () => clearTimeout(t)
  }, [wsConnected])

  // 启动看门狗：若长时间未能连上后台（如 daemon 冷启动失败 / 环境缺失依赖），
  // 避免无尽「加载中」，给出明确错误与重试入口，而不是让用户干等。
  useEffect(() => {
    if (wsConnected || error) return
    const t = setTimeout(() => {
      if (!wsConnected && !error) {
        setError(
          '连接后台服务超时。请确认 Python 环境已安装依赖（pip install -e ".[dev]"），' +
            '并在开发者工具控制台查看 daemon 启动日志。',
        )
      }
    }, 20000)
    return () => clearTimeout(t)
  }, [wsConnected, error])

  useEffect(() => {
    if (!config) return
    // 项目根完全由用户「打开文件夹」决定（持久化在 localStorage），不做任何 env / 默认根兜底。
    const root = loadProjectRoot('')
    setProjectRoot(root)
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

  // 拖拽改宽（仅侧栏）。
  const startResize = () => (e: React.MouseEvent): void => {
    e.preventDefault()
    const startX = e.clientX
    const startW = sidebarW
    const onMove = (ev: MouseEvent): void => {
      const delta = ev.clientX - startX
      const next = clamp(startW + delta, 180, 460)
      setSidebarW(next)
      localStorage.setItem('workagent.sidebarW', String(next))
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
        setLeftNav((n) => (n === 'traces' ? 'chat' : 'traces'))
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
  const skills = useSkills(projectRoot)
  const notices = useNotices(client)
  const obs = useObs(client)

  // M9.9 步骤7：切换会话时清掉上一轮的 Token/耗时 信息。
  useEffect(() => {
    setTurnMeta(null)
  }, [active?.id])

  // 统一 toast 堆叠：通知（来自 daemon）与瞬时提示（如保存成功）共用一个右下角堆叠，避免重叠。
  const [savedToasts, setSavedToasts] = useState<ToastData[]>([])
  const dismissToast = (id: string): void => setSavedToasts((prev) => prev.filter((t) => t.id !== id))
  const pushToast = (kind: ToastKind, text: string): void => {
    const id = `t-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
    setSavedToasts((prev) => [...prev.slice(-3), { id, kind, text }])
  }

  // M9.9 步骤6：会话删除结果反馈（成功提示；失败由 useSessions 重新拉取列表）。
  useEffect(() => {
    if (!client) return
    const off = client.onMessage('session.delete_resp', (env) => {
      const ok = Boolean(env.payload['ok'])
      const err = (env.payload['error'] as string | undefined) ?? '未知错误'
      pushToast(ok ? 'success' : 'error', ok ? '会话已彻底删除' : `删除失败：${err}`)
    })
    return off
  }, [client, pushToast])
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

  // M9.9 步骤4：订阅会话状态与生成生命周期，驱动 Composer 的 plan_mode / 停止按钮。
  useEffect(() => {
    if (!client) return
    const offInfo = client.onMessage('session.info', (env) => {
      const p = env.payload as { plan_mode?: boolean; model?: string }
      setSessionInfo({ planMode: Boolean(p.plan_mode), model: String(p.model ?? '') })
    })
    const offCancelled = client.onMessage('task.cancelled', () => finishTurn())
    const offAttached = client.onMessage('attached', () => finishTurn())
    const offEvent = client.onEvent((ev) => {
      if (ev.type === 'decision') finishTurn()
    })
    // M9.9 步骤7：累计单轮完整用量明细，并实时刷新耗时。
    const offUsage = client.onMessage('usage', (env) => {
      if (!runningRef.current) return
      const payload = env.payload as { usage?: Partial<UsageSummary>; estimated?: boolean }
      const u = payload.usage ?? {}
      addUsage(turnUsageRef.current, u)
      if (payload.estimated) estimatedRef.current = true
      setTurnMeta({
        duration: (Date.now() - turnStartRef.current) / 1000,
        usage: { ...turnUsageRef.current },
      })
    })
    return () => {
      offInfo()
      offCancelled()
      offAttached()
      offEvent()
      offUsage()
    }
  }, [client])

  const submit = (): void => {
    const text = draft.trim()
    if (!text) return
    const slash = parseSlash(text)
    if (slash && client) {
      const cmdName = slash.name.toLowerCase()
      const known = commands.commands.some((c) => c.name.toLowerCase() === cmdName)
      // 识别「斜杠名即技能名」（/skillname prompt）与规范形式（/skill name prompt）：
      // 先加载技能，再把 prompt 作为任务发给 agent 触发回复。
      let skillName: string | undefined
      let prompt = slash.args
      if (known && cmdName === 'skill') {
        const sp = slash.args.indexOf(' ')
        skillName = (sp < 0 ? slash.args : slash.args.slice(0, sp)).trim() || undefined
        prompt = sp < 0 ? '' : slash.args.slice(sp + 1).trim()
      } else if (!known) {
        const s = skills.find((x) => x.name.toLowerCase() === cmdName)
        if (s) {
          skillName = s.name
          prompt = slash.args
        }
      }
      if (skillName) {
        client.command('skill', skillName)
        if (prompt) {
          turnStartRef.current = Date.now()
          turnUsageRef.current = emptyUsage()
          estimatedRef.current = false
          setTurnMeta(null)
          sessions.sendTask(prompt)
          setRunning(true)
        } else {
          pushToast('info', `已加载技能：${skillName}`)
        }
        setDraft('')
        return
      }
      client.command(slash.name, slash.args ? slash.args : null)
    } else {
      turnStartRef.current = Date.now()
      turnUsageRef.current = emptyUsage()
      estimatedRef.current = false
      setTurnMeta(null)
      sessions.sendTask(text)
      setRunning(true)
    }
    setDraft('')
  }

  // M9.9 步骤4：停止生成（真实中断 LLM 流）。
  const handleStop = (): void => {
    client?.cancelTask()
  }

  // M9.9 步骤4：计划 / 执行模式分段切换（乐观更新 + 发命令 + 提示）。
  const handleTogglePlan = (): void => {
    const next = !sessionInfo.planMode
    setSessionInfo((s) => ({ ...s, planMode: next }))
    commands.run(next ? 'plan' : 'exec')
    pushToast('info', next ? '已切换到计划模式（仅规划，不执行）' : '已切换到执行模式')
  }

  // M9.9 顶栏菜单动作（真实功能）。
  const clearCurrent = (): void => {
    if (client && active) client.command('clear', null)
    else pushToast('info', '当前没有打开的会话')
  }
  const helpPlaceholder = (): void => pushToast('info', '帮助即将上线（占位）')

  // 启动遮罩进度步骤：随后台阶段 / 配置就绪 / WebSocket 连上推进。
  const splashSteps: SplashStep[] | undefined = error
    ? undefined
    : [
        { label: '启动后台进程', state: stage === 'spawning' ? 'active' : 'done' },
        {
          label: '等待后台就绪',
          state: config || stage === 'ready' ? 'done' : stage === 'waiting' ? 'active' : 'pending',
        },
        { label: '建立实时连接', state: wsConnected ? 'done' : config ? 'active' : 'pending' },
      ]

  return (
    <div className="wa-app">
      {/* 启动遮罩：后台未连上前全屏覆盖，展示 Logo / 产品名 / 连接进度（消除白屏）。连上后淡出。 */}
      {splashVisible && (
        <SplashScreen
          productName={APP_NAME}
          version={APP_VERSION}
          steps={splashSteps}
          error={error}
          onRetry={() => window.location.reload()}
        />
      )}
      {/* M9.9 自绘顶栏：Logo + 应用名 + 真实功能菜单 + 窗口控制 */}
      <TitleBar
        appName={APP_NAME}
        clearDisabled={!active}
        onClear={clearCurrent}
        onHelp={helpPlaceholder}
      />

      <div className="wa-app__body">
      {/* 侧栏：可折叠 + 可拖拽伸缩（M9.9 重排为 Logo/搜索/导航/工作空间/历史/用户栏） */}
      <Sidebar
        appName={APP_NAME}
        version={APP_VERSION}
        projectRoot={projectRoot}
        width={sidebarW}
        list={sessions.state.list}
        activeId={sessions.state.activeId}
        nav={leftNav}
        collapsed={sidebarCollapsed}
        onNav={setLeftNav}
        onOpen={sessions.openSession}
        onCreate={() => sessions.createSession()}
        onFork={sessions.forkSession}
        onDelete={sessions.deleteSession}
        onSettings={() => setSettingsOpen(true)}
        onCollapse={() =>
          setSidebarCollapsed((v) => {
            const nv = !v
            localStorage.setItem('workagent.sidebarCollapsed', nv ? '1' : '0')
            return nv
          })
        }
        onUser={() => pushToast('info', '账户管理即将上线（占位）')}
      />
      <div className="wa-resizer" onMouseDown={startResize()} />

      {/* 主区：按左侧导航切换 聊天 / 技能 / 智能体 / 可观测 */}
      <main className="wa-main">
        {leftNav === 'chat' && (
          <>
            <SessionTabs
              tabs={sessions.state.tabs}
              activeId={sessions.state.activeId}
              onSwitch={sessions.switchSession}
              onClose={sessions.closeTab}
            />
            <section style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
              {error && <p style={{ color: 'var(--wa-danger)', padding: '0 16px' }}>错误：{error}</p>}
              {!client && <p style={{ color: 'var(--wa-text-muted)', padding: '0 16px' }}>正在连接 daemon…</p>}
              {client && !active && (
                <p style={{ color: 'var(--wa-text-muted)', padding: 16 }}>从左侧新建或打开一个会话开始。</p>
              )}
              {active && (
                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
                  <p style={{ color: 'var(--wa-text-muted)', fontSize: 13, padding: '0 16px', margin: '8px 0' }}>
                    会话 <code>{active.name}</code> · {active.events.length} 条事件
                    {sessions.state.replaying ? '（回放中…）' : ''}
                  </p>
                  <MessageList model={model} autoScroll turnMeta={turnMeta} />
                </div>
              )}
            </section>
            {active && (
              <Composer
                draft={draft}
                onDraftChange={setDraft}
                onSubmit={submit}
                onStop={handleStop}
                running={running}
                planMode={sessionInfo.planMode}
                onTogglePlan={handleTogglePlan}
                model={sessionInfo.model}
                contextTokens={obs.usage?.prompt_tokens}
                contextWindow={contextWindow}
                onShowSkills={() => setLeftNav('skills')}
                commands={commands.commands}
                skills={skills}
                disabled={hitlPending}
              />
            )}
          </>
        )}
        {leftNav === 'skills' && <SkillsPanel client={client} onClose={() => setLeftNav('chat')} />}
        {leftNav === 'agents' && <AgentsPanel client={client} onClose={() => setLeftNav('chat')} />}
        {leftNav === 'traces' && (
          <div className="wa-main__obs">
            <ObsPanel
              client={client}
              projectRoot={projectRoot}
              sessionId={active ? active.id : null}
              contextWindow={contextWindow}
              onClose={() => setLeftNav('chat')}
            />
          </div>
        )}
      </main>

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
    </div>
  )
}
