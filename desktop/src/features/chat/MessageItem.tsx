// 单条文本/用户/错误/澄清/计划块渲染。reasoning 与 content 分栏，思考过程可折叠。

import React, { useState } from 'react'
import type { ChatBlock } from './useEventReducer'
import { Markdown } from './Markdown'

export function MessageItem({ block }: { block: ChatBlock }): React.ReactElement | null {
  switch (block.type) {
    case 'text': {
      const [showReason, setShowReason] = useState(false)
      const hasReason = block.reasoning.trim().length > 0
      return (
        <div className="wa-msg wa-msg-assistant" style={{ margin: '10px 0' }}>
          {hasReason && (
            <div style={{ marginBottom: 6 }}>
              <button type="button" onClick={() => setShowReason((v) => !v)} style={{ fontSize: 12, color: '#888' }}>
                {showReason ? '▾ 收起思考过程' : '▸ 查看思考过程'}
              </button>
              {showReason && (
                <div className="wa-reasoning" style={{ borderLeft: '3px solid #d0d7de', paddingLeft: 10, color: '#57606a', fontSize: 13, whiteSpace: 'pre-wrap' }}>
                  {block.reasoning}
                </div>
              )}
            </div>
          )}
          {block.content.trim().length > 0 ? (
            <Markdown text={block.content} />
          ) : (
            <span style={{ color: '#aaa' }}>（等待输出…）</span>
          )}
        </div>
      )
    }
    case 'user':
      return (
        <div className="wa-msg wa-msg-user" style={{ margin: '10px 0', textAlign: 'right' }}>
          <div style={{ display: 'inline-block', background: '#e7f0ff', padding: '8px 12px', borderRadius: 10, textAlign: 'left', maxWidth: '80%', whiteSpace: 'pre-wrap' }}>
            {block.text}
          </div>
        </div>
      )
    case 'error':
      return (
        <div className="wa-msg wa-msg-error" style={{ margin: '10px 0', color: '#c0392b', background: '#fdecea', padding: 10, borderRadius: 8 }}>
          ⚠ {block.text}
        </div>
      )
    case 'clarify':
      return (
        <div className="wa-msg wa-msg-clarify" style={{ margin: '10px 0', background: '#fff8e1', padding: 10, borderRadius: 8 }}>
          <strong>需要澄清：</strong>
          <ul style={{ margin: '6px 0 0' }}>
            {block.questions.map((q, i) => (
              <li key={i}>
                {q.question}
                {q.options && q.options.length > 0 ? `（选项：${q.options.join(' / ')}）` : ''}
              </li>
            ))}
          </ul>
        </div>
      )
    case 'plan':
      return (
        <div className="wa-msg wa-msg-plan" style={{ margin: '10px 0', background: '#f3e8ff', padding: 10, borderRadius: 8 }}>
          📋 计划{block.status ? ` · ${block.status}` : ''}
          {block.note ? `：${block.note}` : ''}
          {block.planPath ? <div style={{ fontSize: 12, color: '#888' }}>{block.planPath}</div> : null}
        </div>
      )
    default:
      return null
  }
}
