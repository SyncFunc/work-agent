// 按 seq 顺序渲染事件归约出的视图模型块（文本 / 工具卡 / 用户 / 错误 / 澄清 / 计划）。
// 整轮响应分组（Bug4 修复）：顶层连续的助手块被归并进 ResponseBlock，主聊天区统一共享一个
// 助手头像；user 块作为独立分隔符。子 agent 卡作为组内子元素保持完整不被拆开。
// 自动滚动：仅主聊天区（autoScroll=true）启用——贴底且用户上滚时暂停并显示「回到底部」。

import React, { useEffect, useRef, useState } from 'react'
import type { ChatBlock, ChatModel, ResponseBlock } from './useEventReducer'
import type { TurnMeta } from './MessageItem'
import { MessageItem } from './MessageItem'
import { ToolBlock } from './ToolBlock'
import { SubagentCard } from './SubagentCard'
import { Avatar } from '../../components'
import { IconButton } from '../../components'
import { ArrowDown } from 'lucide-react'
import './chat.css'

/** 共享的单块渲染：顶层与 ResponseGroup 组内都用它，保证 tool/subagent/response 处理逻辑一致。
 * inGroup=true 时文本/警示块以 bare 模式渲染（不重复头像/外层），由组容器统一提供。 */
function renderBlock(
  b: ChatBlock,
  opts: { inGroup: boolean; turnMeta: TurnMeta | null; defaultToolCollapsed: boolean },
): React.ReactElement {
  switch (b.type) {
    case 'subagent':
      return <SubagentCard key={b.key} block={b} />
    case 'tool':
      return <ToolBlock key={b.key} block={b} defaultCollapsed={opts.defaultToolCollapsed} />
    case 'user':
      return <MessageItem key={b.key} block={b} turnMeta={opts.turnMeta} />
    case 'response':
      return (
        <ResponseGroup
          key={b.key}
          block={b}
          turnMeta={opts.turnMeta}
          defaultToolCollapsed={opts.defaultToolCollapsed}
        />
      )
    default:
      return <MessageItem key={b.key} block={b} bare={opts.inGroup} turnMeta={opts.turnMeta} />
  }
}

/** 整轮响应组：统一一个助手头像 + 角色头，组内按序渲染 text/tool/subagent/error/clarify/plan。
 * turnMeta 仅挂到组内最后一条文本块（单轮 Token/耗时）。 */
function ResponseGroup({
  block,
  turnMeta,
  defaultToolCollapsed,
}: {
  block: ResponseBlock
  turnMeta: TurnMeta | null
  defaultToolCollapsed: boolean
}): React.ReactElement {
  let lastTextIdx = -1
  block.blocks.forEach((b, i) => {
    if (b.type === 'text') lastTextIdx = i
  })
  return (
    <div className="wa-msg">
      <span className="wa-msg__avatar">
        <Avatar kind="assistant" size={26} />
      </span>
      <div className="wa-msg__main">
        <div className="wa-msg__head">
          <span className="wa-msg__role">助手</span>
        </div>
        {block.blocks.map((b, i) =>
          renderBlock(b, {
            inGroup: true,
            turnMeta: i === lastTextIdx ? turnMeta : null,
            defaultToolCollapsed,
          }),
        )}
      </div>
    </div>
  )
}

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
  /** M9.9 步骤7：单轮 Token / 耗时，仅挂到最后一条助手响应组的最后一条文本块。 */
  turnMeta?: TurnMeta | null
}): React.ReactElement {
  const scrollRef = useRef<HTMLDivElement>(null)
  const atBottomRef = useRef(true)
  const [showJump, setShowJump] = useState(false)

  // M9.9 步骤7：定位最后一个「助手响应组」，把 Token/耗时 仅挂到它内部的最后一条文本块。
  let lastRespKey: string | null = null
  model.blocks.forEach((b) => {
    if (b.type === 'response') lastRespKey = b.key
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
      {model.blocks.map((b) => {
        const tm = b.type === 'response' && b.key === lastRespKey ? turnMeta : null
        return renderBlock(b, { inGroup: false, turnMeta: tm, defaultToolCollapsed })
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
