import React, { useEffect } from 'react'
import { AlertTriangle, CheckCircle2, Info, X } from 'lucide-react'

export type ToastKind = 'info' | 'error' | 'success'

export interface ToastData {
  id: string
  kind: ToastKind
  text: string
}

export interface ToastProps {
  toast: ToastData
  onDismiss: (id: string) => void
  /** 覆盖自动消失时长（ms）。 */
  duration?: number
}

const ICONS: Record<ToastKind, React.ReactNode> = {
  info: <Info size={16} />,
  error: <AlertTriangle size={16} />,
  success: <CheckCircle2 size={16} />,
}

const DEFAULT_DURATION: Record<ToastKind, number> = {
  info: 4000,
  success: 4000,
  error: 6000,
}

/** 单条 Toast：类型图标 + 自动消失 + 关闭按钮。 */
export function Toast({ toast, onDismiss, duration }: ToastProps): React.ReactElement {
  const ms = duration ?? DEFAULT_DURATION[toast.kind]
  useEffect(() => {
    const t = window.setTimeout(() => onDismiss(toast.id), ms)
    return () => window.clearTimeout(t)
  }, [toast.id, ms, onDismiss])

  return (
    <div className={`wa-toast wa-toast--${toast.kind}`} role="status">
      <span className="wa-toast__icon">{ICONS[toast.kind]}</span>
      <span className="wa-toast__msg">{toast.text}</span>
      <button type="button" className="wa-toast__close" aria-label="关闭通知" onClick={() => onDismiss(toast.id)}>
        <X size={14} />
      </button>
    </div>
  )
}

export interface ToastStackProps {
  toasts: ToastData[]
  onDismiss: (id: string) => void
  duration?: number
}

/** 右下角 Toast 堆叠：最多显示 4 条。 */
export function ToastStack({ toasts, onDismiss, duration }: ToastStackProps): React.ReactElement | null {
  if (toasts.length === 0) return null
  return (
    <div className="wa-toast-stack">
      {toasts.slice(0, 4).map((t) => (
        <Toast key={t.id} toast={t} onDismiss={onDismiss} duration={duration} />
      ))}
    </div>
  )
}
