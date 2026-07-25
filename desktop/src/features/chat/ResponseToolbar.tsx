import { useCallback, useState } from 'react'
import {
  Check,
  Clock,
  Copy,
  FileText,
  MoreHorizontal,
  ThumbsDown,
  ThumbsUp,
  Zap,
} from 'lucide-react'
import type { TurnMeta } from './MessageItem'
import './ResponseToolbar.css'

interface Props {
  text: string
  turnMeta?: TurnMeta | null
  onCopy?: () => void
  onLike?: () => void
  onDislike?: () => void
}

const fmtK = (n: number): string =>
  n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : `${(n / 1_000).toFixed(1)}k`

const fmt = (n: number): string => Math.round(n).toLocaleString('en-US')

/** 每行左侧彩色方块定义 */
const ROW_COLORS: Record<string, string> = {
  '输入': '#3b82f6',
  '缓存命中': '#2fa36b',
  '缓存未命中': '#e0a000',
  '缓存写入': '#8b5cf6',
  '输出': 'var(--wa-primary)',
  '思考过程': '#d946ef',
  '回复内容': '#14b8a6',
}

/** 单轮 Token 消耗明细（悬浮胶囊时展示）。 */
function UsagePopover({ meta }: { meta: TurnMeta }): React.ReactElement {
  const u = meta.usage
  const input = u.prompt_tokens
  const hit = u.cache_hit_tokens
  const miss = u.cache_miss_tokens
  const write = u.cache_write_tokens
  const output = u.completion_tokens
  const reasoning = u.reasoning_tokens
  const reply = Math.max(0, u.completion_tokens - u.reasoning_tokens)
  const total = u.total_tokens || input + output
  const rate = input > 0 ? hit / input : 0
  const pct = (n: number) => (input > 0 ? (n / input) * 100 : 0)

  const Row = ({ label, value, indent = false }: { label: string; value: number; indent?: boolean }) => (
    <div className={`wa-tokenpop__row${indent ? ' is-sub' : ''}`}>
      <span className="wa-tokenpop__row-label">
        <span
          className="wa-tokenpop__dot"
          style={{ backgroundColor: ROW_COLORS[label] || 'var(--wa-text-muted)' }}
        />
        {label}
      </span>
      <span className="wa-tokenpop__row-val">{fmt(value)}</span>
    </div>
  )

  return (
    <div className="wa-tokenpop" role="tooltip">
      <div className="wa-tokenpop__head">
        <span>Token 消耗明细</span>
        <span className="wa-tokenpop__dur">{meta.duration.toFixed(1)}s</span>
      </div>

      <div className="wa-tokenpop__total">
        <span className="wa-tokenpop__total-label">总计</span>
        <span className="wa-tokenpop__total-val">{fmt(total)}</span>
      </div>

      <Row label="输入" value={input} />
      <Row label="缓存命中" value={hit} indent />
      <Row label="缓存未命中" value={miss} indent />
      <Row label="缓存写入" value={write} indent />
      <Row label="输出" value={output} />
      <Row label="思考过程" value={reasoning} indent />
      <Row label="回复内容" value={reply} indent />

      <div className="wa-tokenpop__cache">
        <div className="wa-tokenpop__cache-top">
          <span className="wa-tokenpop__cache-label">
            <Zap size={13} style={{ color: 'var(--wa-primary)' }} />
            缓存命中率
          </span>
          <span className="wa-tokenpop__cache-val">{(rate * 100).toFixed(1)}%</span>
        </div>
        <div className="wa-tokenpop__bar">
          <div
            className="wa-tokenpop__bar-fill"
            style={{ width: `${Math.min(100, pct(hit))}%` }}
          />
          <div
            className="wa-tokenpop__bar-write"
            style={{ width: `${Math.min(100, pct(write))}%` }}
          />
        </div>
      </div>
    </div>
  )
}

export function ResponseToolbar({
  text,
  turnMeta,
  onCopy,
  onLike,
  onDislike,
}: Props): React.ReactElement | null {
  const [copied, setCopied] = useState(false)
  const [vote, setVote] = useState<'up' | 'down' | null>(null)

  const copy = useCallback(() => {
    navigator.clipboard?.writeText(text).then(
      () => {
        setCopied(true)
        onCopy?.()
        window.setTimeout(() => setCopied(false), 1500)
      },
      () => {},
    )
  }, [text, onCopy])

  const like = useCallback(() => {
    setVote((v) => (v === 'up' ? null : 'up'))
    onLike?.()
  }, [onLike])
  const dislike = useCallback(() => {
    setVote((v) => (v === 'down' ? null : 'down'))
    onDislike?.()
  }, [onDislike])

  const meta = turnMeta
  const totalTokens = meta ? (meta.usage.total_tokens || meta.usage.prompt_tokens + meta.usage.completion_tokens) : 0

  return (
    <div className="wa-rtoolbar">
      <div className="wa-rtoolbar__actions">
        <button
          type="button"
          className={`wa-rtoolbar__btn${copied ? ' is-active' : ''}`}
          title="复制"
          onClick={copy}
        >
          {copied ? <Check size={15} /> : <Copy size={15} />}
          <span>{copied ? '已复制' : '复制'}</span>
        </button>
        <button
          type="button"
          className={`wa-rtoolbar__btn${vote === 'up' ? ' is-active' : ''}`}
          title="有帮助"
          onClick={like}
        >
          <ThumbsUp size={15} />
          <span>赞</span>
        </button>
        <button
          type="button"
          className={`wa-rtoolbar__btn${vote === 'down' ? ' is-active' : ''}`}
          title="没帮助"
          onClick={dislike}
        >
          <ThumbsDown size={15} />
          <span>踩</span>
        </button>
        <button
          type="button"
          className="wa-rtoolbar__btn wa-rtoolbar__btn--more"
          title="更多功能（建设中）"
        >
          <MoreHorizontal size={15} />
        </button>
      </div>

      {meta ? (
        <div className="wa-rtoolbar__usage">
          <button type="button" className="wa-tokenpill" aria-label="Token 消耗明细">
            <FileText size={14} className="wa-tokenpill__icon" />
            <span className="wa-tokenpill__val">{fmtK(totalTokens)}</span>
            <UsagePopover meta={meta} />
          </button>
          <span className="wa-rtoolbar__usage-sep" />
          <button type="button" className="wa-durpill" aria-label="耗时" title={`耗时 ${meta.duration.toFixed(1)}s`}>
            <Clock size={14} className="wa-durpill__icon" />
            <span className="wa-durpill__val">{meta.duration.toFixed(1)}s</span>
          </button>
        </div>
      ) : null}
    </div>
  )
}
