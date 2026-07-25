import { useEffect, useRef, useState } from 'react'
import { Minus, Square, X } from 'lucide-react'
import './TitleBar.css'

export interface TitleBarProps {
  appName: string
  clearDisabled: boolean
  onClear: () => void
  onHelp: () => void
}

type MenuKey = 'file' | 'edit' | 'window' | 'help'

interface MenuItem {
  label: string
  onClick?: () => void
  disabled?: boolean
}

// M9.9 自绘顶栏：CSS Logo + 应用名 + 真实功能菜单（文件/编辑/窗口/帮助）+ 窗口控制按钮。
// 菜单项均为真实功能：文件→打开文件夹/退出；编辑→清空当前会话；窗口→关闭/重新加载；帮助→占位。
export function TitleBar({ appName, clearDisabled, onClear, onHelp }: TitleBarProps): React.ReactElement {
  const [openMenu, setOpenMenu] = useState<MenuKey | null>(null)
  const barRef = useRef<HTMLDivElement>(null)

  const close = (): void => setOpenMenu(null)

  useEffect(() => {
    const onDown = (e: MouseEvent): void => {
      if (barRef.current && !barRef.current.contains(e.target as Node)) close()
    }
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') close()
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [])

  const menus: Record<MenuKey, { label: string; items: MenuItem[] }> = {
    file: {
      label: '文件',
      items: [
        { label: '打开文件夹…', onClick: () => window.agentApi.openFolder() },
        { label: '退出', onClick: () => window.agentApi.quitApp() },
      ],
    },
    edit: {
      label: '编辑',
      items: [{ label: '清空当前会话', onClick: onClear, disabled: clearDisabled }],
    },
    window: {
      label: '窗口',
      items: [
        { label: '关闭窗口', onClick: () => window.agentApi.closeWindow() },
        { label: '重新加载窗口', onClick: () => window.agentApi.reloadWindow() },
      ],
    },
    help: {
      label: '帮助',
      items: [{ label: '关于 Work Agent（占位）', onClick: onHelp }],
    },
  }

  const api = window.agentApi

  return (
    <div className="wa-titlebar" ref={barRef} onDoubleClick={() => api.toggleMaximizeWindow()}>
      <div className="wa-titlebar__left">
        {/* CSS 绘制 Logo（无图片资源） */}
        <span className="wa-logo" aria-hidden>
          <span className="wa-logo__mark" />
        </span>
        <span className="wa-titlebar__name">{appName}</span>
        <nav className="wa-menubar">
          {(Object.keys(menus) as MenuKey[]).map((key) => {
            const m = menus[key]
            const isOpen = openMenu === key
            return (
              <div className="wa-menu" key={key}>
                <button
                  type="button"
                  className={`wa-menu__trigger${isOpen ? ' is-open' : ''}`}
                  onClick={() => setOpenMenu(isOpen ? null : key)}
                >
                  {m.label}
                </button>
                {isOpen && (
                  <div className="wa-menu__dropdown" role="menu">
                    {m.items.map((it) => (
                      <button
                        type="button"
                        key={it.label}
                        role="menuitem"
                        className="wa-menu__item"
                        disabled={it.disabled}
                        onClick={() => {
                          close()
                          it.onClick?.()
                        }}
                      >
                        {it.label}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </nav>
      </div>
      <div className="wa-titlebar__right">
        <button type="button" className="wa-winbtn" title="最小化" onClick={() => api.minimizeWindow()}>
          <Minus size={14} />
        </button>
        <button type="button" className="wa-winbtn" title="最大化/还原" onClick={() => api.toggleMaximizeWindow()}>
          <Square size={12} />
        </button>
        <button type="button" className="wa-winbtn wa-winbtn--close" title="关闭" onClick={() => api.closeWindow()}>
          <X size={14} />
        </button>
      </div>
    </div>
  )
}
