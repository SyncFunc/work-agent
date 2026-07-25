// SubagentCard：主聊天区（含后台面板）里子 agent 的独立卡片。
// 顶栏显示 `subagent:<name>`，主体固定高度、可滚动；内部工具调用默认折叠（避免刷屏）。

import React from 'react'
import type { SubagentBlock } from './useEventReducer'
import { MessageList } from './MessageList'

export function SubagentCard({
  block,
  status,
}: {
  block: SubagentBlock
  /** 可选状态徽标（后台面板传入 running/done；主聊天区历史不传）。 */
  status?: 'running' | 'done'
}): React.ReactElement {
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        margin: '8px 0',
        border: '1px solid #e0e0e0',
        borderRadius: 6,
        overflow: 'hidden',
        background: '#fafafa',
      }}
    >
      <div
        style={{
          display: 'flex',
          gap: 6,
          alignItems: 'center',
          padding: '6px 8px',
          background: '#eef1f5',
          borderBottom: '1px solid #e0e0e0',
          fontSize: 13,
        }}
      >
        <span style={{ fontWeight: 600, fontFamily: 'monospace' }}>subagent:{block.name}</span>
        {status && (
          <span
            style={{
              marginLeft: 'auto',
              color: status === 'running' ? '#1a73e8' : '#1e7e34',
            }}
          >
            {status === 'running' ? '运行中' : '已完成'}
          </span>
        )}
      </div>
      <div style={{ height: 220, overflowY: 'auto', padding: 6 }}>
        <MessageList model={{ blocks: block.blocks }} defaultToolCollapsed />
      </div>
    </div>
  )
}
