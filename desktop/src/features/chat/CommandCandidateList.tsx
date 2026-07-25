import { useEffect, useRef } from 'react'
import { Command, Sparkles } from 'lucide-react'

export interface CandItem {
  kind: 'command' | 'skill'
  name: string
  description: string
}

export interface CandGroup {
  label: string
  items: CandItem[]
}

interface Props {
  groups: CandGroup[]
  activeIndex: number
  onHover: (index: number) => void
  onPick: (item: CandItem) => void
}

export function CommandCandidateList({ groups, activeIndex, onHover, onPick }: Props) {
  if (groups.length === 0) return null
  const activeRef = useRef<HTMLButtonElement | null>(null)
  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex])
  let counter = 0
  return (
    <div className="wa-cand" role="listbox">
      {groups.map((group) => (
        <div key={group.label} className="wa-cand__group">
          <div className="wa-cand__group-label">{group.label}</div>
          {group.items.map((item) => {
            const idx = counter++
            const active = idx === activeIndex
            return (
              <button
                key={`${item.kind}:${item.name}`}
                ref={active ? activeRef : undefined}
                type="button"
                role="option"
                aria-selected={active}
                className={`wa-cand__item wa-cand__item--${item.kind}${active ? ' wa-cand__item--active' : ''}`}
                onMouseEnter={() => onHover(idx)}
                onMouseDown={(e) => {
                  e.preventDefault()
                  onPick(item)
                }}
              >
                <span className="wa-cand__icon">
                  {item.kind === 'skill' ? <Sparkles size={15} /> : <Command size={15} />}
                </span>
                <span className="wa-cand__text">
                  <span className="wa-cand__name">/{item.name}</span>
                  {item.description ? (
                    <span className="wa-cand__desc">{item.description}</span>
                  ) : null}
                </span>
              </button>
            )
          })}
        </div>
      ))}
    </div>
  )
}
