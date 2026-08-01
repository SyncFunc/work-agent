import { useEffect, useMemo, useRef, useState } from 'react'
import { ListTodo, Play, Send, Square, Wrench } from 'lucide-react'
import { Button, Textarea } from '../../components'
import type { CommandDef } from '../command/useCommands'
import type { SkillInfo } from '../settings/settingsApi'
import { CommandCandidateList, type CandGroup, type CandItem } from './CommandCandidateList'
import './Composer.css'

export interface ComposerProps {
  draft: string
  onDraftChange: (v: string) => void
  onSubmit: () => void
  disabled?: boolean
  /** 真实中断 LLM 流。 */
  onStop?: () => void
  running?: boolean
  planMode?: boolean
  onTogglePlan?: () => void
  /** 当前会话模型（仅展示）。 */
  model?: string
  contextTokens?: number
  contextWindow?: number
  onShowSkills?: () => void
  /** 命令注册表（用于候选框过滤）。 */
  commands: CommandDef[]
  /** 可用技能列表（用于候选框过滤，与命令上下分组展示）。 */
  skills: SkillInfo[]
}

export function Composer(props: ComposerProps): React.ReactElement {
  const {
    draft,
    onDraftChange,
    onSubmit,
    disabled,
    onStop,
    running,
    planMode,
    onTogglePlan,
    model,
    contextTokens,
    contextWindow,
    onShowSkills,
    commands,
    skills,
  } = props

  const taRef = useRef<HTMLTextAreaElement | null>(null)
  const [candOpen, setCandOpen] = useState(true)
  const [candIndex, setCandIndex] = useState(0)

  // 仅「以 / 开头且尚未换行的查询」才弹出候选框。
  const query = draft.startsWith('/') && !draft.includes('\n') ? draft.slice(1).toLowerCase() : null

  const groups = useMemo<CandGroup[]>(() => {
    if (query === null) return []
    const q = query
    const cmdItems: CandItem[] = commands
      .filter(
        (c) =>
          c.name.toLowerCase().includes(q) || (c.description ?? '').toLowerCase().includes(q),
      )
      .map((c) => ({ kind: 'command', name: c.name, description: c.description ?? '' }))
    const skillItems: CandItem[] = skills
      .filter(
        (s) =>
          s.name.toLowerCase().includes(q) || (s.description ?? '').toLowerCase().includes(q),
      )
      .map((s) => ({ kind: 'skill', name: s.name, description: s.description ?? s.when_to_use ?? '' }))
    const g: CandGroup[] = []
    if (cmdItems.length) g.push({ label: '命令', items: cmdItems })
    if (skillItems.length) g.push({ label: '技能', items: skillItems })
    return g
  }, [query, commands, skills])

  const flatCount = groups.reduce((n, g) => n + g.items.length, 0)
  const showCand = query !== null && flatCount > 0 && candOpen

  useEffect(() => {
    if (candIndex >= flatCount) setCandIndex(0)
  }, [flatCount, candIndex])

  const pick = (item: CandItem): void => {
    onDraftChange(`/${item.name} `)
    setCandOpen(false)
    setCandIndex(0)
    requestAnimationFrame(() => taRef.current?.focus())
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>): void => {
    if (showCand) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setCandIndex((i) => (i + 1) % flatCount)
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setCandIndex((i) => (i - 1 + flatCount) % flatCount)
        return
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        if (!e.nativeEvent.isComposing) {
          e.preventDefault()
          const flat = groups.flatMap((g) => g.items)
          pick(flat[candIndex])
        }
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        setCandOpen(false)
        return
      }
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      if (!disabled && draft.trim().length > 0) onSubmit()
    }
  }

  const handleChange = (v: string): void => {
    onDraftChange(v)
    setCandOpen(v.startsWith('/') && !v.includes('\n'))
  }

  const sendDisabled = disabled || draft.trim().length === 0

  const onModeClick = (mode: 'exec' | 'plan'): void => {
    if ((mode === 'plan') !== planMode) onTogglePlan?.()
  }

  const used = contextTokens ?? 0
  const total = contextWindow ?? 0
  const ctxPct = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0

  return (
    <div className="wa-composer">
      {showCand ? (
        <CommandCandidateList
          groups={groups}
          activeIndex={candIndex}
          onHover={setCandIndex}
          onPick={pick}
        />
      ) : null}

      <div className="wa-composer__row">
        <Textarea
          ref={taRef}
          className="wa-composer__input"
          placeholder="给 Work Agent 下达任务…（Enter 发送 · Shift+Enter 换行 · / 唤起命令与技能）"
          value={draft}
          disabled={disabled}
          onChange={handleChange}
          onKeyDown={onKeyDown}
          rows={3}
        />
      </div>

      <div className="wa-composer__footer">
        <div className="wa-composer__footer-left">
          <div className="wa-seg" role="tablist" aria-label="运行模式">
            <button
              type="button"
              role="tab"
              aria-selected={!planMode}
              className={`wa-seg__item${!planMode ? ' wa-seg__item--active' : ''}`}
              onClick={() => onModeClick('exec')}
            >
              <Play size={13} />
              执行
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={planMode}
              className={`wa-seg__item${planMode ? ' wa-seg__item--active' : ''}`}
              onClick={() => onModeClick('plan')}
            >
              <ListTodo size={13} />
              计划
            </button>
          </div>
        </div>
        <div className="wa-composer__footer-right">
          {model ? <CtxRing model={model} used={used} total={total} pct={ctxPct} /> : null}
          {focusedSkillNote(query, skills) ? (
            <span className="wa-composer__hint">{focusedSkillNote(query, skills)}</span>
          ) : null}
          {onShowSkills ? (
            <button
              type="button"
              className="wa-btn wa-btn--ghost"
              onClick={onShowSkills}
              title="查看技能"
            >
              <Wrench size={13} />
              技能
            </button>
          ) : null}
          <Button
            variant={running ? 'danger' : 'primary'}
            size="sm"
            onClick={running ? onStop ?? onSubmit : onSubmit}
            disabled={!running && sendDisabled}
            title={running ? '停止生成' : '发送（Enter）'}
          >
            {running ? <Square size={15} /> : <Send size={15} />}
          </Button>
        </div>
      </div>
    </div>
  )
}

/** 当查询像是某个技能名前缀时，提示「回车注入技能」。 */
function focusedSkillNote(query: string | null, skills: SkillInfo[]): string {
  if (!query) return ''
  const match = skills.find((s) => s.name.toLowerCase().startsWith(query))
  return match ? ' · 选中技能回车注入 / 后接 prompt' : ''
}

/** 上下文占用：圆环进度，hover 才显示具体数字（单位 k）。 */
function CtxRing({ model, used, total, pct }: {
  model: string
  used: number
  total: number
  pct: number
}): React.ReactElement {
  const r = 9
  const circ = 2 * Math.PI * r
  const offset = circ * (1 - pct / 100)
  const color = pct >= 90 ? 'var(--wa-danger)' : pct >= 70 ? '#e0a000' : 'var(--wa-primary)'
  const fmtK = (v: number): string =>
    v >= 1_000_000 ? `${(v / 1_000_000).toFixed(1)}M`
      : v >= 1_000 ? `${(v / 1_000).toFixed(1)}k`
        : String(v)
  return (
    <div className="wa-ctx-ring" tabIndex={0} title={`${model} · ${pct}% ${fmtK(used)} / ${fmtK(total)} 上下文占用`}>
      <svg className="wa-ctx-ring__svg" width="22" height="22" viewBox="0 0 22 22">
        <circle className="wa-ctx-ring__track" cx="11" cy="11" r={r} />
        <circle
          className="wa-ctx-ring__bar"
          cx="11"
          cy="11"
          r={r}
          style={{ stroke: color, strokeDasharray: circ, strokeDashoffset: offset }}
        />
      </svg>
      {model ? <span className="wa-ctx-ring__model">{model}</span> : null}
      <span className="wa-ctx-ring__tip">
        {pct}% · {fmtK(used)} / {fmtK(total)} 上下文占用
      </span>
    </div>
  )
}
