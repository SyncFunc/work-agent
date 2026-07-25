// 从 tool_result.output 中识别 unified diff 并着色（增绿 / 删红 / 上下文灰）；
// 非 diff 文本走普通代码高亮。轻量实现，不引入额外 diff 库。

import React from 'react'

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

export function DiffView({ text }: { text: string }): React.ReactElement {
  const lines = text.split('\n')
  return (
    <pre className="wa-diff" style={{ margin: 0, padding: 8, overflowX: 'auto', fontSize: 13 }}>
      {lines.map((ln, i) => {
        const cls =
          ln.startsWith('+') && !ln.startsWith('+++')
            ? 'wa-diff-add'
            : ln.startsWith('-') && !ln.startsWith('---')
              ? 'wa-diff-del'
              : ln.startsWith('@@')
                ? 'wa-diff-hunk'
                : 'wa-diff-ctx'
        return (
          <div key={i} className={cls}>
            {ln || ' '}
          </div>
        )
      })}
    </pre>
  )
}
