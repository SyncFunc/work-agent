import React from 'react'

export interface SkeletonProps {
  width?: number | string
  height?: number | string
  radius?: number | string
  className?: string
}

/** 骨架占位：连接中/回放中等加载态替代灰字。 */
export function Skeleton({
  width = '100%',
  height = 12,
  radius,
  className = '',
}: SkeletonProps): React.ReactElement {
  return (
    <span
      className={`wa-skeleton ${className}`.trim()}
      style={{ width, height, borderRadius: radius ?? 'var(--wa-r-sm)' }}
      aria-hidden="true"
    />
  )
}
