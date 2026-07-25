// 单条文本/用户/错误/澄清/计划块渲染。reasoning 与 content 分栏，思考过程可折叠。
// 接入设计 token：头像、图标、警示块、streaming 光标全部读 tokens.css 变量。

import React, { useState } from 'react'
import type { ChatBlock } from './useEventReducer'
import { Markdown } from './Markdown'
import { Avatar } from '../../components'
import { AlertTriangle, ChevronRight, ClipboardList, HelpCircle } from 'lucide-react'

export function MessageItem({ block }: { block: ChatBlock }): React.ReactElement | null {
  switch (block.type) {
    case 'text': {
      const [showReason, setShowReason] = useState(false)
      const hasReason = block.reasoning.trim().length > 0
      const streaming = block.content.trim().length === 0
      return (
        <div className="wa-msg">
          <span className="wa-msg__avatar">
            <Avatar kind="assistant" size={26} />
          </span>
          <div className="wa-msg__main">
            <div className="wa-msg__head">
              <span className="wa-msg__role">助手</span>
            </div>
            {hasReason && (
              <button
                type="button"
                className={`wa-reason-toggle ${showReason ? 'wa-reason-toggle--open' : ''}`}
                onClick={() => setShowReason((v) => !v)}
              >
                <ChevronRight size={14} className="wa-reason-toggle__chev" />
                {showReason ? '收起思考过程' : '查看思考过程'}
              </button>
            )}
            {showReason && hasReason && <div className="wa-reasoning">{block.reasoning}</div>}
            {streaming ? (
              <span className="wa-cursor" aria-label="生成中" />
            ) : (
              <Markdown text={block.content} />
            )}
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
