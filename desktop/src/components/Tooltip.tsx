import React, { useState } from 'react'

export interface TooltipProps {
  content: React.ReactNode
  children: React.ReactNode
}

/** 轻量 tooltip：hover / focus 显示，替代裸 title。 */
export function Tooltip({ content, children }: TooltipProps): React.ReactElement {
  const [show, setShow] = useState(false)
  return (
    <span
      className="wa-tooltip-wrap"
      onMouseEnter={() => setShow(true)}
      onMouseLeave={() => setShow(false)}
      onFocus={() => setShow(true)}
      onBlur={() => setShow(false)}
    >
      {children}
      {show && (
        <span className="wa-tooltip" role="tooltip">
          {content}
        </span>
      )}
    </span>
  )
}
