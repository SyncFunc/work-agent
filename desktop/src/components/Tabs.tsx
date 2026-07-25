import React from 'react'

export interface TabItem {
  id: string
  label: React.ReactNode
  icon?: React.ReactNode
}

export interface TabsProps {
  tabs: TabItem[]
  active: string
  onChange: (id: string) => void
  variant?: 'underline' | 'pill'
  className?: string
}

/** 标签页：underline / pill 两种风格，支持左右方向键切换。 */
export function Tabs({
  tabs,
  active,
  onChange,
  variant = 'underline',
  className = '',
}: TabsProps): React.ReactElement {
  const onKey = (e: React.KeyboardEvent, idx: number): void => {
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault()
      const dir = e.key === 'ArrowRight' ? 1 : -1
      const next = (idx + dir + tabs.length) % tabs.length
      onChange(tabs[next].id)
    }
  }
  return (
    <div className={`wa-tabs wa-tabs--${variant} ${className}`.trim()} role="tablist">
      {tabs.map((t, i) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          aria-selected={t.id === active}
          tabIndex={t.id === active ? 0 : -1}
          className={`wa-tab ${t.id === active ? 'wa-tab--active' : ''}`.trim()}
          onClick={() => onChange(t.id)}
          onKeyDown={(e) => onKey(e, i)}
        >
          {t.icon}
          {t.label}
        </button>
      ))}
    </div>
  )
}
