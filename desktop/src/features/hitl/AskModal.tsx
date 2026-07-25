// 澄清提问模态：单选/多选（question.options）或自由输入 → answer(id, text)。

import React, { useState } from 'react'
import type { AskRequest } from './hitlMachine'
import { ApprovalBadge } from './ApprovalBadge'
import { Button, Modal } from '../../components'

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
    <Modal
      open
      title={
        <span>
          需要澄清 <ApprovalBadge risk="safe" />
        </span>
      }
      onClose={() => onAnswer(req.id, '')}
      footer={
        <Button
          variant="primary"
          onClick={submit}
          disabled={options.length > 0 ? selected.length === 0 : !free.trim()}
        >
          发送
        </Button>
      }
    >
      <p>{q.question}</p>
      {options.length > 0 ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--wa-s2)' }}>
          {options.map((opt) => (
            <label key={opt} style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 6 }}>
              <input
                type={multi ? 'checkbox' : 'radio'}
                name={`ask-${req.id}`}
                checked={selected.includes(opt)}
                onChange={() => toggle(opt)}
              />
              {opt}
            </label>
          ))}
        </div>
      ) : (
        <textarea
          className="wa-textarea"
          value={free}
          onChange={(e) => setFree(e.target.value)}
          placeholder="输入你的回答…"
          rows={3}
        />
      )}
    </Modal>
  )
}
