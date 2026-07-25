// 沙箱档位可视化（状态栏/设置展示，M9.6 接入实际配置）。
// profile: read-only / workspace-write / danger-full（来自 settings.sandbox.profile）。
// 用设计 token 语义色表达档位含义，避免硬编码品牌色、暗色模式自动适配。

import React from 'react'
import { Badge } from '../../components'
import type { BadgeTone } from '../../components'

const PROFILE_TONE: Record<string, { tone: BadgeTone; label: string }> = {
  'read-only': { tone: 'success', label: '只读沙箱' },
  'workspace-write': { tone: 'warn', label: '工作区可写' },
  'danger-full': { tone: 'danger', label: '无沙箱(危险)' },
}

export function SandboxViz({ profile }: { profile: string | null | undefined }): React.ReactElement {
  const p = PROFILE_TONE[profile ?? ''] ?? { tone: 'neutral' as BadgeTone, label: profile || '未知档位' }
  return <Badge tone={p.tone}>{p.label}</Badge>
}
