import React from 'react'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
export type ButtonSize = 'sm' | 'md'

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
}

/** 统一按钮：primary/secondary/ghost/danger × sm/md；自带 hover/active/disabled 与 focus-visible。 */
export function Button({
  variant = 'secondary',
  size = 'md',
  className = '',
  type = 'button',
  ...rest
}: ButtonProps): React.ReactElement {
  return (
    <button
      type={type}
      className={`wa-btn wa-btn--${variant} wa-btn--${size} ${className}`.trim()}
      {...rest}
    />
  )
}
