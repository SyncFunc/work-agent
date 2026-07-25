// 命令面板：Ctrl/Cmd+K 唤起，搜索并执行斜杠命令（发 DaemonClient.command）。

import React, { useEffect, useMemo, useRef, useState } from 'react'
import type { CommandDef } from './useCommands'
import { Modal } from '../../components'
import {
  Activity,
  Bot,
  CheckCheck,
  ClipboardList,
  GitFork,
  HelpCircle,
  History,
  Layers,
  Minimize2,
  PlayCircle,
  Repeat,
  Search,
  Sparkles,
  Terminal,
  TerminalSquare,
} from 'lucide-react'

const ICONS: Record<string, React.ReactNode> = {
  context: <Activity size={15} />,
  compact: <Minimize2 size={15} />,
  plan: <ClipboardList size={15} />,
  skills: <Sparkles size={15} />,
  agents: <Bot size={15} />,
  mode: <Repeat size={15} />,
  exec: <TerminalSquare size={15} />,
  approve: <CheckCheck size={15} />,
  bg: <Layers size={15} />,
  sessions: <History size={15} />,
  resume: <PlayCircle size={15} />,
  fork: <GitFork size={15} />,
  skill: <Sparkles size={15} />,
  agent: <Bot size={15} />,
  help: <HelpCircle size={15} />,
}
const FALLBACK_ICON = <Terminal size={15} />

export function CommandPalette({
  commands,
  onRun,
  onClose,
}: {
  commands: CommandDef[]
  onRun: (name: string, args: string) => void
  onClose: () => void
}): React.ReactElement {
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return commands
    return commands.filter((c) => c.name.includes(q) || c.description.toLowerCase().includes(q))
  }, [commands, query])

  useEffect(() => {
    setActive(0)
  }, [query])

  const choose = (c: CommandDef | undefined): void => {
    if (!c) return
    onRun(c.name, '')
    onClose()
  }

  return (
    <Modal open onClose={onClose} title="命令面板" width={480}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 'var(--wa-s2)' }}>
        <Search size={16} className="wa-icon" style={{ color: 'var(--wa-text-faint)' }} />
        <input
          ref={inputRef}
          className="wa-input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="输入命令…（如 context / compact / skills）"
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') {
              e.preventDefault()
              setActive((a) => Math.min(a + 1, filtered.length - 1))
            } else if (e.key === 'ArrowUp') {
              e.preventDefault()
              setActive((a) => Math.max(a - 1, 0))
            } else if (e.key === 'Enter') {
              e.preventDefault()
              choose(filtered[active])
            } else if (e.key === 'Escape') {
              onClose()
            }
          }}
        />
      </div>
      <ul className="wa-cmd-list">
        {filtered.map((c, i) => (
          <li
            key={c.name}
            className={`wa-cmd-item${i === active ? ' wa-cmd-item--active' : ''}`}
            onMouseEnter={() => setActive(i)}
            onClick={() => choose(c)}
          >
            <span className="wa-icon" style={{ color: 'var(--wa-text-muted)' }}>
              {ICONS[c.name] ?? FALLBACK_ICON}
            </span>
            <code>/{c.name}</code>
            <span className="wa-cmd-item__desc">{c.description}</span>
          </li>
        ))}
        {filtered.length === 0 && <li className="wa-cmd-empty">无匹配命令</li>}
      </ul>
    </Modal>
  )
}
