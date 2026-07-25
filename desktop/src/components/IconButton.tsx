import React from 'react'

export type IconButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
export type IconButtonSize = 'sm' | 'md'

export interface IconButtonProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'aria-label'> {
  icon: React.ReactNode
  /** 必填：同时作为 aria-label 与 title，保证可达性。 */
  label: string
  variant?: IconButtonVariant
  size?: IconButtonSize
}

/** 图标按钮：必须提供 label（aria-label）。用于顶栏、关闭、刷新等。 */
export function IconButton({
  icon,
  label,
  variant = 'ghost',
  size = 'md',
  className = '',
  type = 'button',
  ...rest
}: IconButtonProps): React.ReactElement {
  return (
    <button
      type={type}
      aria-label={label}
      title={label}
      className={`wa-icon-btn wa-icon-btn--${variant} wa-icon-btn--${size} ${className}`.trim()}
      {...rest}
    >
      {icon}
    </button>
  )
}
