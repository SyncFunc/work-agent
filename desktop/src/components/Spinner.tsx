import React from 'react'

export interface SpinnerProps {
  size?: number
  className?: string
}

/** 加载指示：统一旋转环，替代「运行中…」等纯文字。 */
export function Spinner({ size = 16, className = '' }: SpinnerProps): React.ReactElement {
  return (
    <span
      className={`wa-spinner ${className}`.trim()}
      style={{ width: size, height: size }}
      role="status"
      aria-label="加载中"
    />
  )
}
