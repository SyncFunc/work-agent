// 工具调用卡 — 按工具类型分派到专用渲染组件：
//   bash    → BashBlock（终端风）
//   write/edit → DiffBlock（固定高度 diff，完成后自动折叠）
//   read    → ReadBlock（默认折叠，显示路径+行数）
//   present_plan → PlanGeneratingBlock（呼吸动画）
//   update_plan  → PlanProgressBlock（完整计划列表，图标状态，高亮本次更新）
//   grep/其他 → GenericToolBlock

import React, { useEffect, useRef, useState } from 'react'
import type { ToolBlock as ToolBlockModel } from './useEventReducer'
import { DiffView, isDiffLike } from './DiffView'
import { Badge, Button, IconButton, Spinner } from '../../components'
import {
  CheckCircle2,
  ChevronRight,
  ClipboardList,
  Copy,
  FileText,
  Terminal,
  Wrench,
  XCircle,
} from 'lucide-react'
import { PlanStepList, STATUS_LABEL } from './PlanStepList'

const OUTPUT_LIMIT = 2000

// ══════════════════════════════════════════
//  BashBlock
// ══════════════════════════════════════════
function BashBlock({ block }: { block: ToolBlockModel }): React.ReactElement {
  const [expanded, setExpanded] = useState(true)
  const [showAll, setShowAll] = useState(false)
  const cmd = (block.args?.cmd ?? '') as string
  const result = block.result
  const out = result ? (result.output ?? result.error ?? '') : ''
  const failed = result ? !result.ok : false
  const truncated = out.length > OUTPUT_LIMIT
  const shownOut = truncated && !showAll ? out.slice(0, OUTPUT_LIMIT) + '\n…(已截断，点击展开)' : out

  return (
    <div className="wa-tool wa-tool-bash">
      <button type="button" className="wa-tool__head" onClick={() => setExpanded((v) => !v)}>
        <Terminal size={14} />
        <code className="wa-tool-bash__cmd">$ {cmd || block.name}</code>
        {block.running ? (
          <Spinner size={12} />
        ) : failed ? (
          <XCircle size={14} className="wa-clr-danger" />
        ) : result ? (
          <CheckCircle2 size={14} className="wa-clr-success" />
        ) : null}
        <ChevronRight size={16} className={`wa-tool__chev ${expanded ? 'wa-tool__chev--open' : ''}`} />
      </button>
      {expanded && out && <pre className="wa-tool-bash__output">{shownOut}</pre>}
      {truncated && (
        <div style={{ padding: '0 var(--wa-s3) var(--wa-s1)' }}>
          <Button size="sm" onClick={() => setShowAll((v) => !v)}>
            {showAll ? '收起' : '展开全部'}
          </Button>
        </div>
      )}
    </div>
  )
}

// ══════════════════════════════════════════
//  DiffBlock (write / edit)
// ══════════════════════════════════════════
function DiffBlock({ block }: { block: ToolBlockModel }): React.ReactElement {
  const [expanded, setExpanded] = useState(true)
  const [showAll, setShowAll] = useState(false)
  const result = block.result
  // 优先使用 diff 字段（完整 diff 内容），回退到 output
  const content = (result?.diff ?? result?.output ?? result?.error ?? '')
  const out = content
  const failed = result ? !result.ok : false
  const truncated = out.length > OUTPUT_LIMIT
  const shownOut = truncated && !showAll ? out.slice(0, OUTPUT_LIMIT) + '\n…(已截断，点击展开)' : out
  const filePath = (block.args?.path ?? '') as string

  // 写入完成后自动折叠（仅执行一次），用户可手动展开查看
  const doneRef = useRef(false)
  useEffect(() => {
    if (!block.running && block.result?.ok && !doneRef.current) {
      doneRef.current = true
      setExpanded(false)
    }
  }, [block.running, block.result?.ok])

  return (
    <div className="wa-tool wa-tool-diff">
      <button type="button" className="wa-tool__head" onClick={() => setExpanded((v) => !v)}>
        <Wrench size={14} />
        <span className="wa-tool__name">{block.name}</span>
        {filePath && <code className="wa-tool-diff__path">{filePath}</code>}
        <span style={{ flex: 1 }} />
        {block.running ? (
          <span className="wa-tool-diff__status">
            <Spinner size={12} />
            <span className="wa-tool-diff__status-text">写入中…</span>
          </span>
        ) : failed ? (
          <XCircle size={14} className="wa-clr-danger" />
        ) : result?.ok ? (
          <CheckCircle2 size={14} className="wa-clr-success" />
        ) : null}
        <ChevronRight size={16} className={`wa-tool__chev ${expanded ? 'wa-tool__chev--open' : ''}`} />
      </button>
      {expanded && out && (
        <div className="wa-tool-diff__body">
          {isDiffLike(out) ? (
            <DiffView text={shownOut} />
          ) : (
            <pre className="wa-tool-diff__out">{shownOut}</pre>
          )}
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
  )
}

// ══════════════════════════════════════════
//  ReadBlock (read)
// ══════════════════════════════════════════
function ReadBlock({ block }: { block: ToolBlockModel }): React.ReactElement {
  const [expanded, setExpanded] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const filePath = (block.args?.path ?? '') as string
  const offset = block.args?.offset as number | undefined
  const limit = block.args?.limit as number | undefined
  const result = block.result
  const out = result ? (result.output ?? result.error ?? '') : ''
  const failed = result ? !result.ok : false
  const truncated = out.length > OUTPUT_LIMIT
  const shownOut = truncated && !showAll ? out.slice(0, OUTPUT_LIMIT) + '\n…(已截断，点击展开)' : out

  // 从 args 中解析行范围；若 LLM 未传 offset/limit，从结果 header 中回退提取
  const lineEnd = offset != null && limit != null ? offset + limit - 1 : undefined
  const lineRange = offset != null
    ? `L${offset}${lineEnd != null ? `-${lineEnd}` : ''}`
    : (result?.output?.match(/lines\s+(\d+-\d+)/)?.[1] ?? '')

  return (
    <div className="wa-tool wa-tool-read">
      <button type="button" className="wa-tool__head" onClick={() => setExpanded((v) => !v)}>
        <FileText size={14} />
        <span className="wa-tool__name">{block.name}</span>
        {filePath && <code className="wa-tool-read__path">{filePath}</code>}
        {lineRange && <span className="wa-tool-read__range">{lineRange}</span>}
        {block.running ? (
          <Spinner size={12} />
        ) : failed ? (
          <XCircle size={14} className="wa-clr-danger" />
        ) : result ? (
          <CheckCircle2 size={14} className="wa-clr-success" />
        ) : null}
        <ChevronRight size={16} className={`wa-tool__chev ${expanded ? 'wa-tool__chev--open' : ''}`} />
      </button>
      {expanded && out && (
        <div className="wa-tool__body">
          {isDiffLike(out) ? <DiffView text={shownOut} /> : <pre className="wa-result">{shownOut}</pre>}
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
  )
}

// ══════════════════════════════════════════
//  PlanGeneratingBlock (present_plan)
// ══════════════════════════════════════════
function PlanGeneratingBlock({ block }: { block: ToolBlockModel }): React.ReactElement {
  return (
    <div className="wa-tool wa-plan-gen">
      <div className="wa-plan-gen__pulse" />
      <div className="wa-plan-gen__text">
        {block.running ? (
          <>
            <span>正在生成计划</span>
            <span className="wa-plan-gen__dots">
              <span>.</span><span>.</span><span>.</span>
            </span>
          </>
        ) : block.result?.ok ? (
          <span className="wa-clr-success">计划已生成</span>
        ) : (
          <span className="wa-clr-danger">计划生成失败</span>
        )}
      </div>
    </div>
  )
}

// ══════════════════════════════════════════
//  PlanProgressBlock (update_plan)
// ══════════════════════════════════════════
function PlanProgressBlock({ block }: { block: ToolBlockModel }): React.ReactElement {
  const upd = block.planUpdate
  const steps = block.planSteps
  const failed = block.result ? !block.result.ok : false
  return (
    <div className="wa-tool wa-plan-progress">
      <div className="wa-plan-progress__head">
        <ClipboardList size={14} className="wa-plan-progress__icon" />
        <span className="wa-tool__name">update_plan</span>
        {upd ? (
          <span className={`wa-badge wa-badge--${upd.status}`}>
            {upd.stepId} → {STATUS_LABEL[upd.status] ?? upd.status}
          </span>
        ) : null}
        <span style={{ flex: 1 }} />
        {block.running ? (
          <Spinner size={12} />
        ) : failed ? (
          <XCircle size={14} className="wa-clr-danger" />
        ) : (
          <CheckCircle2 size={14} className="wa-clr-success" />
        )}
      </div>
      {upd?.note ? <div className="wa-plan-progress__note">{upd.note}</div> : null}
      {steps && steps.length > 0 ? (
        <div className="wa-plan-progress__body">
          <PlanStepList steps={steps} highlightId={upd?.stepId} />
        </div>
      ) : null}
    </div>
  )
}

// ══════════════════════════════════════════
//  GenericToolBlock（grep / 其他工具）
// ══════════════════════════════════════════
function GenericToolBlock({
  block,
  defaultCollapsed,
}: {
  block: ToolBlockModel
  defaultCollapsed: boolean
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

  const statusPill = block.running ? (
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
        {statusPill}
        <ChevronRight size={16} className={`wa-tool__chev ${expanded ? 'wa-tool__chev--open' : ''}`} />
      </button>
      {expanded && (
        <div className="wa-tool__body">
          <div className="wa-tool__label">
            参数 {previewing ? '（流式预览）' : hasFinalArgs ? '' : '（未提供）'}
          </div>
          <div className="wa-code-wrap">
            <pre className="wa-params">{paramsText}</pre>
            <IconButton
              icon={<Copy size={14} />}
              label="复制参数"
              size="sm"
              className="wa-copy-btn"
              onClick={() => copy(paramsText)}
            />
          </div>
          {result && (
            <div style={{ marginTop: 'var(--wa-s2)' }}>
              <div className="wa-tool__label">结果</div>
              <div className="wa-code-wrap">
                {isDiffLike(out) ? (
                  <DiffView text={shownOut} />
                ) : (
                  <pre className="wa-result">{shownOut || '(空)'}</pre>
                )}
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

// ══════════════════════════════════════════
//  Main export
// ══════════════════════════════════════════
export function ToolBlock({
  block,
  defaultCollapsed = false,
}: {
  block: ToolBlockModel
  /** 默认折叠（子 agent 块内默认折叠，避免刷屏；主会话默认展开）。 */
  defaultCollapsed?: boolean
}): React.ReactElement {
  if (block.name === 'bash') return <BashBlock block={block} />
  if (block.name === 'present_plan') return <PlanGeneratingBlock block={block} />
  if (block.name === 'update_plan') return <PlanProgressBlock block={block} />
  if (block.name === 'write' || block.name === 'edit') return <DiffBlock block={block} />
  if (block.name === 'read') return <ReadBlock block={block} />
  // grep 默认折叠；其他工具沿用父级 defaultCollapsed（由 MessageList opts.defaultToolCollapsed 传入）
  return <GenericToolBlock block={block} defaultCollapsed={block.name === 'grep' || defaultCollapsed} />
}
