import React from 'react'

export interface SegProps {
  options: string[]
  value: string
  onChange: (value: string) => void
  className?: string
}

/** 分段控件（模式切换 / 作用域切换等）。选项即展示文案与取值。 */
export function Seg({ options, value, onChange, className }: SegProps): React.ReactElement {
  return (
    <div className={`wa-seg${className ? ` ${className}` : ''}`} role="radiogroup">
      {options.map((opt) => (
        <button
          key={opt}
          type="button"
          role="radio"
          aria-checked={opt === value}
          className={`wa-seg__item${opt === value ? ' wa-seg__item--active' : ''}`}
          onClick={() => onChange(opt)}
        >
          {opt}
        </button>
      ))}
    </div>
  )
}
