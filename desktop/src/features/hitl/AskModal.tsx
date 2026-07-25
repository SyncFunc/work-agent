// 澄清提问模态：单选/多选（question.options）或自由输入 → answer(id, text)。

import React, { useState } from 'react'
import type { AskRequest } from './hitlMachine'
import { ApprovalBadge } from './ApprovalBadge'

export function AskModal({
  req,
  onAnswer,
}: {
  req: AskRequest
  onAnswer: (id: string, text: string) => void
}): React.ReactElement {
  const q = req.question
  const multi = q.multiSelect === true
  const options = q.options ?? []
  const [selected, setSelected] = useState<string[]>([])
  const [free, setFree] = useState('')

  const toggle = (opt: string): void => {
    setSelected((prev) =>
      multi
        ? prev.includes(opt)
          ? prev.filter((o) => o !== opt)
          : [...prev, opt]
        : [opt],
    )
  }

  const submit = (): void => {
    const text = options.length > 0 ? selected.join('; ') : free.trim()
    if (!text) return
    onAnswer(req.id, text)
  }

  return (
    <div className="wa-modal">
      <div className="wa-modal-box">
        <h3 style={{ marginTop: 0 }}>需要澄清 <ApprovalBadge risk="safe" /></h3>
        <p>{q.question}</p>
        {options.length > 0 ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {options.map((opt) => (
              <label key={opt} style={{ cursor: 'pointer' }}>
                <input
                  type={multi ? 'checkbox' : 'radio'}
                  name={`ask-${req.id}`}
                  checked={selected.includes(opt)}
                  onChange={() => toggle(opt)}
                />{' '}
                {opt}
              </label>
            ))}
          </div>
        ) : (
          <textarea
            value={free}
            onChange={(e) => setFree(e.target.value)}
            placeholder="输入你的回答…"
            rows={3}
            style={{ width: '100%', boxSizing: 'border-box' }}
          />
        )}
        <div style={{ marginTop: 12, textAlign: 'right' }}>
          <button type="button" onClick={submit} disabled={options.length > 0 ? selected.length === 0 : !free.trim()}>
            发送
          </button>
        </div>
      </div>
    </div>
  )
}
