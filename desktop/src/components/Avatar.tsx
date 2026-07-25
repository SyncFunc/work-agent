import React from 'react'

export type AvatarKind = 'assistant' | 'user' | 'subagent'

export interface AvatarProps {
  kind: AvatarKind
  /** 展示文本（取首字符大写）；缺省按角色给默认字母。 */
  label?: string
  size?: number
}

/** 角色头像：助手/用户/子 agent 三种配色，统一圆形徽标。 */
export function Avatar({ kind, label, size = 24 }: AvatarProps): React.ReactElement {
  const fallback = kind === 'user' ? 'U' : kind === 'subagent' ? 'S' : 'A'
  const text = label ? label.slice(0, 1).toUpperCase() : fallback
  return (
    <span
      className={`wa-avatar wa-avatar--${kind}`}
      style={{ width: size, height: size, fontSize: Math.round(size * 0.42) }}
      aria-hidden="true"
    >
      {text}
    </span>
  )
}
