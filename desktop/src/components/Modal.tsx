import React, { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'

export interface ModalProps {
  open: boolean
  onClose: () => void
  title?: React.ReactNode
  children: React.ReactNode
  footer?: React.ReactNode
  /** 点击遮罩关闭（默认 true）。 */
  closeOnOverlay?: boolean
  ariaLabel?: string
  /** 覆盖 box 的 max-width。 */
  width?: number | string
}

const FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])'

/** 模态壳：所有弹窗统一经此获得 Esc 关闭 + 焦点陷阱 + 遮罩关闭 + 进出场动画。 */
export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  closeOnOverlay = true,
  ariaLabel,
  width,
}: ModalProps): React.ReactElement | null {
  const boxRef = useRef<HTMLDivElement>(null)
  const prevFocus = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!open) return
    prevFocus.current = document.activeElement as HTMLElement | null
    const box = boxRef.current
    const focusables = box ? Array.from(box.querySelectorAll<HTMLElement>(FOCUSABLE)) : []
    if (focusables.length > 0) focusables[0].focus()
    else box?.focus()

    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onClose()
        return
      }
      if (e.key === 'Tab') {
        const nodes = box ? Array.from(box.querySelectorAll<HTMLElement>(FOCUSABLE)) : []
        if (nodes.length === 0) {
          e.preventDefault()
          return
        }
        const first = nodes[0]
        const last = nodes[nodes.length - 1]
        const active = document.activeElement as HTMLElement
        if (e.shiftKey && active === first) {
          e.preventDefault()
          last.focus()
        } else if (!e.shiftKey && active === last) {
          e.preventDefault()
          first.focus()
        }
      }
    }
    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      prevFocus.current?.focus?.()
    }
  }, [open, onClose])

  if (!open) return null

  return createPortal(
    <div
      className="wa-modal"
      onMouseDown={(e) => {
        if (closeOnOverlay && e.button === 0 && e.target === e.currentTarget) onClose()
      }}
    >
      <div
        className="wa-modal-box"
        ref={boxRef}
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel ?? (typeof title === 'string' ? title : undefined)}
        tabIndex={-1}
        style={width ? { maxWidth: width } : undefined}
      >
        {title != null && (
          <div className="wa-modal-box__head">
            <h3 className="wa-modal-box__title">{title}</h3>
          </div>
        )}
        <div className="wa-modal-box__body">{children}</div>
        {footer != null && <div className="wa-modal-box__foot">{footer}</div>}
      </div>
    </div>,
    document.body,
  )
}
