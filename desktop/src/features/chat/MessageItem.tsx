// 单条文本/用户/错误/澄清/计划块渲染。reasoning 与 content 分栏，思考过程可折叠。
// 接入设计 token：头像、图标、警示块、streaming 光标全部读 tokens.css 变量。

import React, { useLayoutEffect, useRef, useState } from 'react'
import type { ChatBlock } from './useEventReducer'
import { Markdown } from './Markdown'
import { Avatar } from '../../components'
import { ResponseToolbar } from './ResponseToolbar'
import { AlertTriangle, ChevronRight, ClipboardList, HelpCircle } from 'lucide-react'

/** M9.9 步骤7：单轮响应元信息（Token 明细 / 耗时），由主聊天区在最后一条助手消息上挂载。 */
export interface UsageSummary {
  prompt_tokens: number
  completion_tokens: number
  reasoning_tokens: number
  cache_hit_tokens: number
  cache_miss_tokens: number
  cache_write_tokens: number
  total_tokens: number
}

export interface TurnMeta {
  duration: number
  usage: UsageSummary
}

export function MessageItem({
  block,
  turnMeta = null,
}: {
  block: ChatBlock
  /** 仅最后一条助手消息携带：本轮累计 Token / 耗时。 */
  turnMeta?: TurnMeta | null
}): React.ReactElement | null {
  switch (block.type) {
    case 'text': {
      const [showReason, setShowReason] = useState(false)
      const hasReason = block.reasoning.trim().length > 0
      const contentEmpty = block.content.trim().length === 0
      // 仅「当前正在流式生成」的气泡（block.streaming 由归约器在段末标记）需要光标；
      // 被工具/决策冲刷出的中间思考段 streaming=false → 折叠、不带光标，
      // 从而「上一轮思考段」不再被展开、光标只出现在当前轮。
      const streaming = block.streaming
      const showReasoning = streaming || showReason
      // 正文流式阶段：把光标作为持久 DOM 节点钉在「最后一段 markdown 的末尾行」内，
      // 使其像文本编辑器光标一样闪烁在行中（而非落在整块之下的独立行）。
      // 复用同一节点（只 move、不重建），避免每次增量重渲时 blink 动画被重置。
      const mdWrapRef = useRef<HTMLDivElement>(null)
      const caretRef = useRef<HTMLSpanElement | null>(null)
      useLayoutEffect(() => {
        const root = mdWrapRef.current
        if (!root) return
        if (streaming && !contentEmpty) {
          if (!caretRef.current) {
            const c = document.createElement('span')
            c.className = 'wa-cursor'
            c.setAttribute('aria-label', '生成中')
            caretRef.current = c
          }
          const md = root.querySelector('.wa-md') as HTMLElement | null
          const last = md?.lastElementChild as HTMLElement | null
          if (last && caretRef.current.parentElement !== last) last.appendChild(caretRef.current)
        } else if (caretRef.current && caretRef.current.parentElement) {
          caretRef.current.parentElement.removeChild(caretRef.current)
        }
      }, [block.content, streaming, contentEmpty])
      return (
        <div className="wa-msg">
          <span className="wa-msg__avatar">
            <Avatar kind="assistant" size={26} />
          </span>
          <div className="wa-msg__main">
            <div className="wa-msg__head">
              <span className="wa-msg__role">助手</span>
            </div>
            {hasReason && !streaming && (
              <button
                type="button"
                className={`wa-reason-toggle ${showReason ? 'wa-reason-toggle--open' : ''}`}
                onClick={() => setShowReason((v) => !v)}
              >
                <ChevronRight size={14} className="wa-reason-toggle__chev" />
                {showReason ? '收起思考过程' : '查看思考过程'}
              </button>
            )}
            {streaming && contentEmpty ? (
              // 纯思考阶段（尚无正文）：自动展开思考，光标跟随思考末尾
              hasReason ? (
                <div className="wa-reasoning">
                  {block.reasoning}
                  <span className="wa-cursor" aria-label="生成中" />
                </div>
              ) : (
                <span className="wa-cursor" aria-label="生成中" />
              )
            ) : (
              <>
                {showReasoning && hasReason && <div className="wa-reasoning">{block.reasoning}</div>}
                {!contentEmpty && (
                  <div ref={mdWrapRef}>
                    <Markdown text={block.content} />
                  </div>
                )}
              </>
            )}
            {/* M9.9 步骤7：响应工具条（复制/赞/踩）+ Token 胶囊（悬浮看消耗明细） */}
            <ResponseToolbar text={block.content} turnMeta={turnMeta} />
          </div>
        </div>
      )
    }
    case 'user':
      return (
        <div className="wa-msg wa-msg--user">
          <span className="wa-msg__avatar">
            <Avatar kind="user" size={26} />
          </span>
          <div className="wa-msg__main">
            <div className="wa-msg__head">
              <span className="wa-msg__role">你</span>
            </div>
            <div className="wa-bubble">{block.text}</div>
          </div>
        </div>
      )
    case 'error':
      return (
        <div className="wa-msg">
          <div className="wa-alert wa-alert--error" role="alert">
            <span className="wa-alert__icon">
              <AlertTriangle size={16} />
            </span>
            <div>{block.text}</div>
          </div>
        </div>
      )
    case 'clarify':
      return (
        <div className="wa-msg">
          <div className="wa-alert wa-alert--clarify">
            <span className="wa-alert__icon">
              <HelpCircle size={16} />
            </span>
            <div>
              <strong>需要澄清</strong>
              <ul>
                {block.questions.map((q, i) => (
                  <li key={i}>
                    {q.question}
                    {q.options && q.options.length > 0 ? `（选项：${q.options.join(' / ')}）` : ''}
                  </li>
                ))}
              </ul>
              {block.answer !== undefined ? (
                <div className="wa-clarify__answer">
                  <strong>您的回答：</strong>
                  {block.answer}
                </div>
              ) : null}
            </div>
          </div>
        </div>
      )
    case 'plan':
      return (
        <div className="wa-msg">
          <div className="wa-alert wa-alert--plan">
            <span className="wa-alert__icon">
              <ClipboardList size={16} />
            </span>
            <div>
              <strong>计划{block.status ? ` · ${block.status}` : ''}</strong>
              {block.note ? `：${block.note}` : ''}
              {block.planPath ? (
                <div style={{ fontSize: 'var(--wa-f-sm)', opacity: 0.85, marginTop: 2 }}>{block.planPath}</div>
              ) : null}
            </div>
          </div>
        </div>
      )
    default:
      return null
  }
}
