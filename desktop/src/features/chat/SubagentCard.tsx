// SubagentCard：主聊天区（含后台面板）里子 agent 的独立卡片。
// 顶栏 Bot 图标 + subagent:name + 状态 Pill（运行中/已完成）；整卡可折叠避免长会话刷屏。

import React, { useState } from 'react'
import type { SubagentBlock } from './useEventReducer'
import { deriveSubagentStatus } from './useEventReducer'
import { MessageList } from './MessageList'
import { Badge, IconButton } from '../../components'
import { Bot, CheckCircle2, ChevronDown, ChevronRight, Loader } from 'lucide-react'

export function SubagentCard({
  block,
  status,
}: {
  block: SubagentBlock
  /** 可选状态徽标（后台面板传入 running/done；主聊天区由内部块推导）。 */
  status?: 'running' | 'done'
}): React.ReactElement {
  const [collapsed, setCollapsed] = useState(false)

  // 主聊天区无外部 status 时，由子 agent 内部块推导运行状态（存在 final/error 终态即视为完成）。
  const resolvedStatus = status ?? deriveSubagentStatus(block.blocks)

  const statusPill =
    resolvedStatus === 'running' ? (
      <Badge tone="primary" icon={<Loader size={12} />}>
        运行中
      </Badge>
    ) : resolvedStatus === 'done' ? (
      <Badge tone="success" icon={<CheckCircle2 size={12} />}>
        已完成
      </Badge>
    ) : null

  return (
    <div className="wa-subagent">
      <div className="wa-subagent__head">
        <Bot size={15} />
        <span className="wa-subagent__title">subagent:{block.name}</span>
        {statusPill}
        <IconButton
          icon={collapsed ? <ChevronRight size={16} /> : <ChevronDown size={16} />}
          label={collapsed ? '展开' : '折叠'}
          size="sm"
          className="wa-subagent__toggle"
          onClick={() => setCollapsed((v) => !v)}
        />
      </div>
      {!collapsed && (
        <div className="wa-subagent__body">
          <MessageList model={{ blocks: block.blocks }} defaultToolCollapsed />
        </div>
      )}
    </div>
  )
}
