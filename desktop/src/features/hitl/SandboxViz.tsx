// 沙箱档位可视化（状态栏/设置展示，M9.6 接入实际配置）。
// profile: read-only / workspace-write / danger-full（来自 settings.sandbox.profile）。

import React from 'react'

const PROFILE_STYLE: Record<string, { bg: string; fg: string; label: string }> = {
  'read-only': { bg: '#e8f5e9', fg: '#2e7d32', label: '只读沙箱' },
  'workspace-write': { bg: '#fff4e5', fg: '#b9770e', label: '工作区可写' },
  'danger-full': { bg: '#fdecea', fg: '#c0392b', label: '无沙箱(危险)' },
}

export function SandboxViz({ profile }: { profile: string | null | undefined }): React.ReactElement {
  const p = PROFILE_STYLE[profile ?? ''] ?? { bg: '#eee', fg: '#555', label: profile || '未知档位' }
  return (
    <span
      style={{
        background: p.bg,
        color: p.fg,
        borderRadius: 10,
        padding: '1px 8px',
        fontSize: 12,
        fontWeight: 600,
      }}
    >
      {p.label}
    </span>
  )
}
