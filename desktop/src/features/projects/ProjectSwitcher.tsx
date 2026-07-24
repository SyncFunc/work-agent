import React, { useEffect, useState } from 'react'

const STORAGE_KEY = 'workagent.projectRoot'

interface Props {
  projectRoot: string
  onChange: (root: string) => void
}

/** 左侧项目根切换：输入目录路径；当前 project_root 持久化到 localStorage。 */
export function ProjectSwitcher({ projectRoot, onChange }: Props): React.ReactElement {
  const [value, setValue] = useState(projectRoot)

  useEffect(() => setValue(projectRoot), [projectRoot])

  const commit = (): void => {
    const v = value.trim()
    if (v && v !== projectRoot) {
      try {
        localStorage.setItem(STORAGE_KEY, v)
      } catch {
        /* localStorage 不可用时忽略 */
      }
      onChange(v)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, padding: '8px 12px' }}>
      <label style={{ fontSize: 12, color: '#666' }}>项目根</label>
      <div style={{ display: 'flex', gap: 4 }}>
        <input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') commit()
          }}
          placeholder="项目绝对路径"
          style={{ flex: 1, minWidth: 0 }}
        />
        <button onClick={commit}>切换</button>
      </div>
    </div>
  )
}

/** 读取持久化的项目根（无则回退默认）。 */
export function loadProjectRoot(fallback: string): string {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    if (v && v.length > 0) return v
  } catch {
    /* ignore */
  }
  return fallback
}
