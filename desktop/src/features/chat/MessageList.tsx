// 按 seq 顺序渲染事件归约出的视图模型块（文本 / 工具卡 / 用户 / 错误 / 澄清 / 计划）。
// 自动滚动：仅主聊天区（autoScroll=true）启用——贴底且用户上滚时暂停并显示「回到底部」。

import React, { useEffect, useRef, useState } from 'react'
import type { ChatModel } from './useEventReducer'
import type { TurnMeta } from './MessageItem'
import { MessageItem } from './MessageItem'
import { ToolBlock } from './ToolBlock'
import { SubagentCard } from './SubagentCard'
import { IconButton } from '../../components'
import { ArrowDown } from 'lucide-react'
import './chat.css'

export function MessageList({
  model,
  defaultToolCollapsed = false,
  autoScroll = false,
  turnMeta = null,
}: {
  model: ChatModel
  /** 工具调用默认折叠（子 agent 块内为 true）。 */
  defaultToolCollapsed?: boolean
  /** 主聊天区启用自动滚动（子 agent 卡内为 false）。 */
  autoScroll?: boolean
  /** M9.9 步骤7：单轮 Token / 耗时，仅挂到最后一条助手文本块。 */
  turnMeta?: TurnMeta | null
}): React.ReactElement {
  const scrollRef = useRef<HTMLDivElement>(null)
  const atBottomRef = useRef(true)
  const [showJump, setShowJump] = useState(false)

  // M9.9 步骤7：定位最后一条「助手文本块」，把 Token/耗时 仅挂到它身上。
  let lastTextIdx = -1
  model.blocks.forEach((b, i) => {
    if (b.type === 'text') lastTextIdx = i
  })

  const scrollToBottom = (): void => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }

  useEffect(() => {
    if (autoScroll && atBottomRef.current) scrollToBottom()
  }, [model.blocks, autoScroll])

  const onScroll = (): void => {
    const el = scrollRef.current
    if (!el) return
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight
    const atBottom = dist < 48
    atBottomRef.current = atBottom
    setShowJump(autoScroll && !atBottom)
  }

  const content = (
    <div className="wa-chat">
      {model.blocks.map((b, i) => {
        if (b.type === 'subagent') return <SubagentCard key={b.key} block={b} />
        if (b.type === 'tool') return <ToolBlock key={b.key} block={b} defaultCollapsed={defaultToolCollapsed} />
        return <MessageItem key={b.key} block={b} turnMeta={i === lastTextIdx ? turnMeta : null} />
      })}
    </div>
  )

  if (model.blocks.length === 0) {
    if (!autoScroll) return <p className="wa-empty">（暂无消息）</p>
    return (
      <div className="wa-empty">
        <ArrowDown size={28} className="wa-empty__icon" />
        <div>还没有消息，发送任务开始对话</div>
      </div>
    )
  }

  if (!autoScroll) return content

  return (
    <div className="wa-chat-scroll" ref={scrollRef} onScroll={onScroll}>
      {content}
      {showJump && (
        <IconButton
          icon={<ArrowDown size={18} />}
          label="回到底部"
          className="wa-jump-bottom"
          onClick={() => {
            atBottomRef.current = true
            setShowJump(false)
            scrollToBottom()
          }}
        />
      )}
    </div>
  )
}
