// 工具调用卡：默认展开。参数（只读 JSON）/ 流式预览 + 结果区（diff 高亮、超长截断+展开）。
// 接入设计 token：Wrench 图标 + 状态 Pill（运行中/完成/失败）+ 复制按钮，移除 emoji。

import React, { useState } from 'react'
import type { ToolBlock as ToolBlockModel } from './useEventReducer'
import { DiffView, isDiffLike } from './DiffView'
import { Badge, Button, IconButton, Spinner } from '../../components'
import { CheckCircle2, ChevronRight, Copy, Wrench, XCircle } from 'lucide-react'

const OUTPUT_LIMIT = 2000

export function ToolBlock({
  block,
  defaultCollapsed = false,
}: {
  block: ToolBlockModel
  /** 默认折叠（子 agent 块内默认折叠，避免刷屏；主会话默认展开）。 */
  defaultCollapsed?: boolean
}): React.ReactElement {
  const [expanded, setExpanded] = useState(!defaultCollapsed)
  const [showAll, setShowAll] = useState(false)
  const [copied, setCopied] = useState(false)

  const hasFinalArgs = block.args !== null
  const paramsText = hasFinalArgs ? JSON.stringify(block.args, null, 2) : block.deltaArgs || '(等待参数…)'
  const previewing = !hasFinalArgs && block.deltaArgs.length > 0

  const result = block.result
  const out = result ? (result.output ?? result.error ?? '') : ''
  const failed = result ? !result.ok : false
  const truncated = out.length > OUTPUT_LIMIT
  const shownOut = truncated && !showAll ? out.slice(0, OUTPUT_LIMIT) + '\n…(已截断，点击展开)' : out

  const copy = (text: string): void => {
    void navigator.clipboard?.writeText(text)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1200)
  }

  const statusPill =
    block.running ? (
      <Badge tone="primary" icon={<Spinner size={12} />}>
        运行中
      </Badge>
    ) : failed ? (
      <Badge tone="danger" icon={<XCircle size={12} />}>
        失败
      </Badge>
    ) : result ? (
      <Badge tone="success" icon={<CheckCircle2 size={12} />}>
        完成
      </Badge>
    ) : null

  return (
    <div className="wa-tool">
      <button type="button" className="wa-tool__head" onClick={() => setExpanded((v) => !v)}>
        <Wrench size={14} />
        <span className="wa-tool__name">{block.name}</span>
        {block.toolCallId ? (
          <code style={{ color: 'var(--wa-text-faint)', fontSize: 'var(--wa-f-xs)' }}>{block.toolCallId.slice(0, 8)}</code>
        ) : null}
        {statusPill}
        <ChevronRight size={16} className={`wa-tool__chev ${expanded ? 'wa-tool__chev--open' : ''}`} />
      </button>
      {expanded && (
        <div className="wa-tool__body">
          <div className="wa-tool__label">参数 {previewing ? '（流式预览）' : hasFinalArgs ? '' : '（未提供）'}</div>
          <div className="wa-code-wrap">
            <pre className="wa-params">{paramsText}</pre>
            <IconButton icon={<Copy size={14} />} label="复制参数" size="sm" className="wa-copy-btn" onClick={() => copy(paramsText)} />
          </div>
          {result && (
            <div style={{ marginTop: 'var(--wa-s2)' }}>
              <div className="wa-tool__label">结果</div>
              <div className="wa-code-wrap">
                {isDiffLike(out) ? <DiffView text={shownOut} /> : <pre className="wa-result">{shownOut || '(空)'}</pre>}
                <IconButton
                  icon={copied ? <CheckCircle2 size={14} /> : <Copy size={14} />}
                  label="复制结果"
                  size="sm"
                  className="wa-copy-btn"
                  onClick={() => copy(out)}
                />
              </div>
              {truncated && (
                <div style={{ marginTop: 'var(--wa-s1)' }}>
                  <Button size="sm" onClick={() => setShowAll((v) => !v)}>
                    {showAll ? '收起' : '展开全部'}
                  </Button>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
