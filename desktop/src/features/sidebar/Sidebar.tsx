import React, { useEffect, useState } from 'react'
import { Sparkles, Bot, Plug, BarChart2, Search, Plus, Settings, PanelLeft, GitBranch, Trash2, Check, X } from 'lucide-react'
import type { SessionInfo } from '../../protocol/types'
import { IconButton, Logo } from '../../components'
import './Sidebar.css'

export type LeftNav = 'chat' | 'skills' | 'agents' | 'mcp' | 'traces'

interface SidebarProps {
  appName: string
  version: string
  projectRoot: string
  width: number
  list: SessionInfo[]
  activeId: string | null
  nav: LeftNav
  collapsed: boolean
  onNav: (n: LeftNav) => void
  onOpen: (id: string) => void
  onCreate: () => void
  onFork: (id: string) => void
  onDelete: (id: string) => void
  onRename: (id: string, title: string) => void
  onSettings: () => void
  onCollapse: () => void
  onUser: () => void
}

function relTime(ts?: number | null): string {
  if (!ts) return ''
  // daemon 下发的 last_activity / updated_at 是秒级 epoch（time.time()），
  // 而 Date.now() 返回毫秒。判断量级后统一换算，避免显示成「2949 周前」。
  const ms = ts < 1e12 ? ts * 1000 : ts
  const diff = Date.now() - ms
  const m = Math.floor(diff / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m}分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}小时前`
  const d = Math.floor(h / 24)
  if (d < 7) return `${d}天前`
  return `${Math.floor(d / 7)}周前`
}

function basename(p: string): string {
  const parts = p.split(/[\\/]/).filter(Boolean)
  return parts.length ? parts[parts.length - 1] : p
}

const NAV: { key: LeftNav; label: string; icon: React.ReactNode }[] = [
  { key: 'skills', label: '技能', icon: <Sparkles size={16} /> },
  { key: 'agents', label: '智能体', icon: <Bot size={16} /> },
  { key: 'mcp', label: 'MCP', icon: <Plug size={16} /> },
  { key: 'traces', label: '可观测', icon: <BarChart2 size={16} /> },
]

// M9.9 左侧栏：Logo+版本 / 搜索 / 新建任务 / 导航菜单(技能·智能体·可观测) /
// 工作空间(basename)+历史(相对时间) / 底部用户栏。
export function Sidebar(props: SidebarProps): React.ReactElement {
  const {
    appName,
    version,
    projectRoot,
    width,
    list,
    activeId,
    nav,
    collapsed,
    onNav,
    onOpen,
    onCreate,
    onFork,
    onDelete,
    onRename,
    onSettings,
    onCollapse,
    onUser,
  } = props
  const [query, setQuery] = useState('')
  // M9.9 步骤6：删除二次确认。
  const [confirmId, setConfirmId] = useState<string | null>(null)
  // M11.6：会话标题编辑（id + 当前输入值）。
  const [editId, setEditId] = useState<string | null>(null)
  const [editValue, setEditValue] = useState('')
  // M11.6：相对时间（「X分钟前」等）需周期刷新，否则不重进页面就不会更新。
  const [, setTick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 30000)
    return () => clearInterval(t)
  }, [])

  if (collapsed) {
    return (
      <aside className="wa-sidebar wa-sidebar--collapsed">
        <div className="wa-brand" style={{ flexDirection: 'column', gap: 4 }}>
          <IconButton icon={<PanelLeft size={18} />} label="展开侧栏" onClick={onCollapse} />
          <IconButton icon={<BarChart2 size={18} />} label="可观测面板" onClick={() => onNav('traces')} />
          <IconButton icon={<Settings size={18} />} label="设置" onClick={onSettings} />
        </div>
      </aside>
    )
  }

  const q = query.trim().toLowerCase()
  const filtered = q ? list.filter((s) => (s.name ?? s.id).toLowerCase().includes(q)) : list

  return (
    <aside className="wa-sidebar" style={{ width }}>
      <div className="wa-brand">
        <span className="wa-brand__logo" aria-hidden>
          <Logo size={26} />
        </span>
        <div className="wa-brand__text">
          <span className="wa-brand__title-text">{appName}</span>
          <span className="wa-brand__version">v{version}</span>
        </div>
        <div className="wa-sidebar-actions">
          <IconButton icon={<Settings size={18} />} label="设置" onClick={onSettings} />
          <IconButton icon={<PanelLeft size={18} />} label="折叠侧栏" onClick={onCollapse} />
        </div>
      </div>

      <div className="wa-sidebar-top">
        <div className="wa-search">
          <Search size={14} className="wa-search__icon" />
          <input
            className="wa-search__input"
            placeholder="搜索任务…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <button type="button" className="wa-newtask" onClick={onCreate}>
          <Plus size={15} /> 新建任务
        </button>
      </div>

      <nav className="wa-nav">
        {NAV.map((n) => (
          <button
            type="button"
            key={n.key}
            className={`wa-nav__item${nav === n.key ? ' is-active' : ''}`}
            onClick={() => onNav(n.key)}
          >
            <span className="wa-nav__icon">{n.icon}</span>
            <span>{n.label}</span>
          </button>
        ))}
      </nav>

      <div className="wa-workspace">
        <div className="wa-workspace__head">工作空间</div>
        <div className="wa-workspace__name" title={projectRoot}>
          {basename(projectRoot) || '—'}
        </div>
      </div>

      <div className="wa-history">
        <div className="wa-history__head">历史任务（{filtered.length}）</div>
        {filtered.length === 0 ? (
          <div className="wa-session-empty">{q ? '无匹配任务' : '暂无历史任务，点击上方「新建任务」开始。'}</div>
        ) : (
          <ul className="wa-history-list">
            {filtered.map((s) => {
              const active = s.id === activeId
              return (
                <li
                  key={s.id}
                  className={`wa-history-item${active ? ' is-active' : ''}`}
                  onClick={() => onOpen(s.id)}
                >
                  <span className="wa-history-item__doc" aria-hidden>
                    <DocIcon />
                  </span>
                  <span className="wa-history-item__main">
                    {editId === s.id ? (
                      <input
                        className="wa-history-item__edit"
                        autoFocus
                        value={editValue}
                        placeholder="输入标题…"
                        onChange={(e) => setEditValue(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            onRename(s.id, editValue.trim())
                            setEditId(null)
                          } else if (e.key === 'Escape') {
                            setEditId(null)
                          }
                        }}
                      />
                    ) : (
                      <span
                        className="wa-history-item__name"
                        title={s.title ?? '新会话'}
                        onClick={(e) => {
                          e.stopPropagation()
                          setEditId(s.id)
                          setEditValue(s.title ?? s.name ?? '')
                        }}
                      >
                        {s.title ?? '新会话'}
                      </span>
                    )}
                    <span className="wa-history-item__time">{relTime(s.last_activity)}</span>
                  </span>
                  {editId === s.id && (
                    <span className="wa-history-item__confirm">
                      <button
                        type="button"
                        className="wa-history-item__confirm-yes"
                        title="确认标题"
                        onClick={(e) => {
                          e.stopPropagation()
                          onRename(s.id, editValue.trim())
                          setEditId(null)
                        }}
                      >
                        <Check size={13} />
                      </button>
                      <button
                        type="button"
                        className="wa-history-item__confirm-no"
                        title="取消"
                        onClick={(e) => {
                          e.stopPropagation()
                          setEditId(null)
                        }}
                      >
                        <X size={13} />
                      </button>
                    </span>
                  )}
                  {s.running && <span className="wa-history-item__running" title="运行中" />}
                  {confirmId === s.id ? (
                    <span
                      className="wa-history-item__confirm"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <button
                        type="button"
                        className="wa-history-item__confirm-yes"
                        title="确认删除"
                        onClick={(e) => {
                          e.stopPropagation()
                          setConfirmId(null)
                          onDelete(s.id)
                        }}
                      >
                        <Check size={13} />
                      </button>
                      <button
                        type="button"
                        className="wa-history-item__confirm-no"
                        title="取消"
                        onClick={(e) => {
                          e.stopPropagation()
                          setConfirmId(null)
                        }}
                      >
                        <X size={13} />
                      </button>
                    </span>
                  ) : (
                    <>
                      <IconButton
                        icon={<GitBranch size={14} />}
                        label="fork 出新会话"
                        size="sm"
                        onClick={(e) => {
                          e.stopPropagation()
                          onFork(s.id)
                        }}
                      />
                      <IconButton
                        icon={<Trash2 size={14} />}
                        label="删除会话"
                        size="sm"
                        onClick={(e) => {
                          e.stopPropagation()
                          setConfirmId(s.id)
                        }}
                      />
                    </>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </div>

      <div className="wa-userbar" onClick={onUser}>
        <span className="wa-userbar__avatar">U</span>
        <span className="wa-userbar__name">本地用户</span>
        <span className="wa-userbar__chevron" aria-hidden>
          ⌄
        </span>
      </div>
    </aside>
  )
}

function DocIcon(): React.ReactElement {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
      <path d="M4 1.5h5L13 5v9.5H4z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
      <path d="M9 1.5V5h4" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
    </svg>
  )
}
