// 按 seq 顺序渲染事件归约出的视图模型块（文本 / 工具卡 / 用户 / 错误 / 澄清 / 计划）。

import React from 'react'
import type { ChatModel } from './useEventReducer'
import { MessageItem } from './MessageItem'
import { ToolBlock } from './ToolBlock'
import { SubagentCard } from './SubagentCard'

export function MessageList({
  model,
  defaultToolCollapsed = false,
}: {
  model: ChatModel
  /** 工具调用默认折叠（子 agent 块内为 true）。 */
  defaultToolCollapsed?: boolean
}): React.ReactElement {
  if (model.blocks.length === 0) {
    return <p style={{ color: '#aaa' }}>（暂无消息）</p>
  }
  return (
    <div>
      {model.blocks.map((b) => {
        if (b.type === 'subagent') return <SubagentCard key={b.key} block={b} />
        if (b.type === 'tool')
          return <ToolBlock key={b.key} block={b} defaultCollapsed={defaultToolCollapsed} />
        return <MessageItem key={b.key} block={b} />
      })}
    </div>
  )
}
