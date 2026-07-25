import React from 'react'

export type BadgeTone = 'primary' | 'success' | 'warn' | 'danger' | 'neutral'

export interface BadgeProps {
  tone?: BadgeTone
  icon?: React.ReactNode
  children: React.ReactNode
  className?: string
}

/** 状态徽标/胶囊：用 tone + 可选图标表达状态，不止颜色（可达性）。 */
export function Badge({ tone = 'neutral', icon, children, className = '' }: BadgeProps): React.ReactElement {
  return (
    <span className={`wa-badge wa-badge--${tone} ${className}`.trim()}>
      {icon ? <span className="wa-badge__icon">{icon}</span> : null}
      {children}
    </span>
  )
}
