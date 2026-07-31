// 从 tool_result.output 中识别 unified diff 并着色（增绿 / 删红 / 上下文灰 / hunk 蓝）；
// 非 diff 文本走普通代码高亮。支持行号、复制补丁、统一/并排两种视图。轻量实现，不引入额外 diff 库。

import React, { useMemo, useState } from 'react'
import { Button, IconButton } from '../../components'
import { Check, Copy } from 'lucide-react'

const DIFF_LINE = /^(?:[+-]\s|@@ |diff --git |index |\+\+\+ |--- )/

export function isDiffLike(text: string): boolean {
  const lines = text.split('\n')
  if (lines.length < 3) return false
  let hits = 0
  for (const ln of lines) {
    if (DIFF_LINE.test(ln)) hits += 1
    if (hits >= 3) return true
  }
  return false
}

function lineClass(ln: string): string {
  if (ln.startsWith('+') && !ln.startsWith('+++')) return 'wa-diff-add'
  if (ln.startsWith('-') && !ln.startsWith('---')) return 'wa-diff-del'
  if (ln.startsWith('@@')) return 'wa-diff-hunk'
  return 'wa-diff-ctx'
}

interface SideLine {
  text: string
  cls: string
}
interface SplitRow {
  left: SideLine | null
  right: SideLine | null
}

// 将 unified diff 的相邻 - / + 行配对成并排行；上下文/hunk 行两侧都出现。
function toSplitRows(lines: string[]): SplitRow[] {
  const rows: SplitRow[] = []
  let delBuf: string[] = []
  let addBuf: string[] = []
  const flush = (): void => {
    const n = Math.max(delBuf.length, addBuf.length)
    for (let k = 0; k < n; k++) {
      const left = delBuf[k]
      const right = addBuf[k]
      rows.push({
        left: left != null ? { text: left, cls: lineClass(left) } : null,
        right: right != null ? { text: right, cls: lineClass(right) } : null,
      })
    }
    delBuf = []
    addBuf = []
  }
  for (const ln of lines) {
    if (ln.startsWith('+') && !ln.startsWith('+++')) addBuf.push(ln)
    else if (ln.startsWith('-') && !ln.startsWith('---')) delBuf.push(ln)
    else {
      flush()
      rows.push({ left: { text: ln, cls: lineClass(ln) }, right: { text: ln, cls: lineClass(ln) } })
    }
  }
  flush()
  return rows
}

export function DiffView({
  text,
  compact = false,
}: {
  text: string
  /** 紧凑模式：隐藏工具栏（并排/统一切换、复制按钮），只渲染 diff 行。write/edit 块用。 */
  compact?: boolean
}): React.ReactElement {
  const [view, setView] = useState<'unified' | 'split'>('unified')
  const [copied, setCopied] = useState(false)
  const lines = useMemo(() => text.split('\n'), [text])
  const splitRows = useMemo(() => toSplitRows(lines), [lines])

  const copy = (): void => {
    void navigator.clipboard
      .writeText(text)
      .then(() => {
        setCopied(true)
        window.setTimeout(() => setCopied(false), 1500)
      })
      .catch(() => {})
  }

  return (
    <div className="wa-diff-view">
      {!compact && (
        <div className="wa-diff-view__bar">
          <Button variant="ghost" size="sm" onClick={() => setView((v) => (v === 'unified' ? 'split' : 'unified'))}>
            {view === 'unified' ? '并排视图' : '统一视图'}
          </Button>
          <IconButton
            icon={copied ? <Check size={14} /> : <Copy size={14} />}
            label={copied ? '已复制' : '复制补丁'}
            size="sm"
            onClick={copy}
          />
        </div>
      )}

      {view === 'unified' ? (
        <pre className="wa-diff">
          {lines.map((ln, i) => (
            <div key={i} className={`wa-diff-row ${lineClass(ln)}`}>
              <span className="wa-diff__num">{i + 1}</span>
              <span className="wa-diff-line">{ln || ' '}</span>
            </div>
          ))}
        </pre>
      ) : (
        <div className="wa-diff wa-diff--split">
          {splitRows.map((r, i) => (
            <div key={i} className="wa-diff-row">
              <div className={`wa-diff-cell${r.left ? ' ' + r.left.cls : ''}`}>
                <span className="wa-diff__num">{i + 1}</span>
                <span className="wa-diff-line">{r.left ? r.left.text || ' ' : ''}</span>
              </div>
              <div className={`wa-diff-cell${r.right ? ' ' + r.right.cls : ''}`}>
                <span className="wa-diff__num">{i + 1}</span>
                <span className="wa-diff-line">{r.right ? r.right.text || ' ' : ''}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
