import { useEffect, useState } from 'react'
import { Bot, Sparkles, X } from 'lucide-react'
import type { DaemonClient } from '../../protocol/client'
import { IconButton } from '../../components'
import './SpecPanels.css'

interface Spec {
  name: string
  description: string
}

interface Props {
  client: DaemonClient | null
  onClose: () => void
}

// M9.9 技能视图：进入即执行 /skills，订阅 show_skills 渲染真实能力清单。
export function SkillsPanel({ client, onClose }: Props): React.ReactElement {
  const [specs, setSpecs] = useState<Spec[] | null>(null)

  useEffect(() => {
    if (!client) return
    setSpecs(null)
    const off = client.onMessage('show_skills', (env) => {
      const arr = (env.payload['specs'] as Array<Record<string, unknown>>) ?? []
      setSpecs(
        arr.map((s) => ({ name: String(s.name ?? '?'), description: String(s.description ?? '') })),
      )
    })
    client.command('skills')
    return off
  }, [client])

  return (
    <SpecView
      title="技能"
      hint="执行 /skills 返回的已注册技能清单。"
      icon={<Sparkles size={18} />}
      specs={specs}
      onClose={onClose}
    />
  )
}

// M9.9 智能体视图：进入即执行 /agents，订阅 show_agents 渲染可用子 Agent。
export function AgentsPanel({ client, onClose }: Props): React.ReactElement {
  const [specs, setSpecs] = useState<Spec[] | null>(null)

  useEffect(() => {
    if (!client) return
    setSpecs(null)
    const off = client.onMessage('show_agents', (env) => {
      const arr = (env.payload['specs'] as Array<Record<string, unknown>>) ?? []
      setSpecs(
        arr.map((s) => ({ name: String(s.name ?? '?'), description: String(s.description ?? '') })),
      )
    })
    client.command('agents')
    return off
  }, [client])

  return (
    <SpecView
      title="智能体"
      hint="执行 /agents 返回的可用子 Agent 清单。"
      icon={<Bot size={18} />}
      specs={specs}
      onClose={onClose}
    />
  )
}

function SpecView({
  title,
  hint,
  icon,
  specs,
  onClose,
}: {
  title: string
  hint: string
  icon: React.ReactNode
  specs: Spec[] | null
  onClose: () => void
}): React.ReactElement {
  return (
    <section className="wa-specview">
      <div className="wa-specview__head">
        <span className="wa-specview__icon">{icon}</span>
        <div className="wa-specview__titles">
          <h2 className="wa-specview__title">{title}</h2>
          <span className="wa-specview__hint">{hint}</span>
        </div>
        <IconButton icon={<X size={16} />} label="返回聊天" onClick={onClose} />
      </div>
      <div className="wa-specview__body">
        {specs === null ? (
          <div className="wa-specview__loading">加载中…</div>
        ) : specs.length === 0 ? (
          <div className="wa-specview__empty">暂无可用{title}</div>
        ) : (
          <ul className="wa-specview__list">
            {specs.map((s) => (
              <li key={s.name} className="wa-spec-card">
                <div className="wa-spec-card__name">{s.name}</div>
                <div className="wa-spec-card__desc">{s.description || '（无描述）'}</div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}
