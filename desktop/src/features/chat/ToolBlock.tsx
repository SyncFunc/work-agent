// 工具调用卡：默认展开。参数（只读 JSON）/ 流式预览 + 结果区（diff 高亮、超长截断+展开）。

import React, { useState } from 'react'
import type { ToolBlock as ToolBlockModel } from './useEventReducer'
import { DiffView, isDiffLike } from './DiffView'

const OUTPUT_LIMIT = 2000

export function ToolBlock({ block }: { block: ToolBlockModel }): React.ReactElement {
  const [expanded, setExpanded] = useState(true)
  const [showAll, setShowAll] = useState(false)

  const hasFinalArgs = block.args !== null
  const paramsText = hasFinalArgs
    ? JSON.stringify(block.args, null, 2)
    : block.deltaArgs || '(等待参数…)'
  const previewing = !hasFinalArgs && block.deltaArgs.length > 0

  const result = block.result
  const out = result ? (result.output ?? result.error ?? '') : ''
  const failed = result ? !result.ok : false
  const truncated = out.length > OUTPUT_LIMIT
  const shownOut = truncated && !showAll ? out.slice(0, OUTPUT_LIMIT) + '\n…(已截断，点击展开)' : out

  return (
    <div className="wa-tool" style={{ border: '1px solid #ddd', borderRadius: 8, margin: '8px 0', overflow: 'hidden' }}>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        style={{ width: '100%', textAlign: 'left', padding: '6px 10px', background: failed ? '#fdecea' : '#f6f8fa', border: 'none', cursor: 'pointer' }}
      >
        <span style={{ fontWeight: 600 }}>🔧 {block.name}</span>
        {block.toolCallId ? <code style={{ marginLeft: 6, color: '#888' }}>{block.toolCallId.slice(0, 8)}</code> : null}
        <span style={{ marginLeft: 8, color: block.running ? '#b8860b' : failed ? '#c0392b' : '#2e7d32' }}>
          {block.running ? '运行中…' : failed ? '失败' : result ? '完成' : ''}
        </span>
        <span style={{ float: 'right', color: '#888' }}>{expanded ? '▾' : '▸'}</span>
      </button>
      {expanded && (
        <div style={{ padding: 10 }}>
          <div style={{ fontSize: 12, color: '#888', marginBottom: 4 }}>
            参数 {previewing ? '（流式预览）' : hasFinalArgs ? '' : '（未提供）'}
          </div>
          <pre className="wa-params" style={{ margin: 0, background: '#fbfbfd', padding: 8, fontSize: 13, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
            {paramsText}
          </pre>
          {result && (
            <div style={{ marginTop: 8 }}>
              <div style={{ fontSize: 12, color: '#888', marginBottom: 4 }}>结果</div>
              {isDiffLike(out) ? (
                <DiffView text={shownOut} />
              ) : (
                <pre className="wa-result" style={{ margin: 0, background: failed ? '#fff5f5' : '#fbfbfd', padding: 8, fontSize: 13, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                  {shownOut || '(空)'}
                </pre>
              )}
              {truncated && (
                <button type="button" onClick={() => setShowAll((v) => !v)} style={{ marginTop: 4 }}>
                  {showAll ? '收起' : '展开全部'}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
