// 按 seq 顺序渲染事件归约出的视图模型块（文本 / 工具卡 / 用户 / 错误 / 澄清 / 计划）。

import React from 'react'
import type { ChatModel } from './useEventReducer'
import { MessageItem } from './MessageItem'
import { ToolBlock } from './ToolBlock'

export function MessageList({ model }: { model: ChatModel }): React.ReactElement {
  if (model.blocks.length === 0) {
    return <p style={{ color: '#aaa' }}>（暂无消息）</p>
  }
  return (
    <div>
      {model.blocks.map((b) =>
        b.type === 'tool' ? (
          <ToolBlock key={b.key} block={b} />
        ) : (
          <MessageItem key={b.key} block={b} />
        ),
      )}
    </div>
  )
}
