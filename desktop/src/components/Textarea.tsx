import React, { forwardRef } from 'react'

export interface TextareaProps {
  value: string
  onChange: (value: string) => void
  onKeyDown?: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void
  onBlur?: (e: React.FocusEvent<HTMLTextAreaElement>) => void
  placeholder?: string
  disabled?: boolean
  rows?: number
  className?: string
}

/** 受控多行输入，onChange 透传字符串值，方便 Composer 直接接管草稿状态。 */
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { value, onChange, onKeyDown, onBlur, placeholder, disabled, rows = 3, className },
  ref,
) {
  return (
    <textarea
      ref={ref}
      className={className}
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      rows={rows}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={onKeyDown}
      onBlur={onBlur}
    />
  )
})
