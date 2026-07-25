// 命令面板：Ctrl/Cmd+K 唤起，搜索并执行斜杠命令（发 DaemonClient.command）。

import React, { useEffect, useMemo, useRef, useState } from 'react'
import type { CommandDef } from './useCommands'

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
    return commands.filter(
      (c) => c.name.includes(q) || c.description.toLowerCase().includes(q),
    )
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
    <div className="wa-modal" onClick={onClose}>
      <div className="wa-modal-box" style={{ maxWidth: 480 }} onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="输入命令…（如 context / compact / skills）"
          style={{ width: '100%', boxSizing: 'border-box', padding: 8 }}
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
        <ul style={{ listStyle: 'none', margin: '8px 0 0', padding: 0, maxHeight: 280, overflow: 'auto' }}>
          {filtered.map((c, i) => (
            <li
              key={c.name}
              onMouseEnter={() => setActive(i)}
              onClick={() => choose(c)}
              style={{
                padding: '6px 8px',
                borderRadius: 6,
                cursor: 'pointer',
                background: i === active ? '#e7f0ff' : 'transparent',
              }}
            >
              <code>/{c.name}</code> <span style={{ color: '#888', fontSize: 13 }}>{c.description}</span>
            </li>
          ))}
          {filtered.length === 0 && <li style={{ color: '#888', padding: 6 }}>无匹配命令</li>}
        </ul>
      </div>
    </div>
  )
}
