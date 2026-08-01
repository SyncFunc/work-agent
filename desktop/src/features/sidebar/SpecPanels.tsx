import { useEffect, useMemo, useState } from 'react'
import { Bot, Check as CheckIcon, Pencil, Sparkles, X } from 'lucide-react'
import type { DaemonClient } from '../../protocol/client'
import { IconButton } from '../../components'
import './SpecPanels.css'

interface Spec {
  name: string
  description: string
  /** M11.6 来源：builtin / user / project（用于分组展示）。 */
  source?: string
  tools?: string[] | null
  disallowed_tools?: string[]
  model?: string | null
  permission_mode?: string | null
  max_turns?: number | null
  panel_height?: number
  /** 技能：模型是否可自动调用（true = 需手动 /name）。 */
  disable_model_invocation?: boolean
  /** 技能：用户是否可手动调用。 */
  user_invocable?: boolean
  /** 智能体：是否内置（内置不可编辑）。 */
  builtin?: boolean
  /** 智能体：是否共享父会话历史。 */
  share_history?: boolean
}

interface Props {
  client: DaemonClient | null
  /** 当前打开的项目根：技能/智能体清单按此项目扫描（M11.6），不依赖是否已 attach 会话。 */
  projectRoot: string
  onClose: () => void
}

type SpecKind = 'skill' | 'agent'

const SOURCE_LABEL: Record<string, string> = {
  builtin: '内置',
  user: '用户级',
  project: '项目级',
}

// M9.9 技能视图：进入即执行 /skills，订阅 show_skills 渲染真实能力清单。
export function SkillsPanel({ client, projectRoot, onClose }: Props): React.ReactElement {
  const [specs, setSpecs] = useState<Spec[] | null>(null)

  useEffect(() => {
    if (!client) return
    setSpecs(null)
    const off = client.onMessage('show_skills', (env) => {
      const arr = (env.payload['specs'] as Array<Record<string, unknown>>) ?? []
      setSpecs(
        arr.map((s) => ({
          name: String(s.name ?? '?'),
          description: String(s.description ?? ''),
          source: String(s.source ?? 'user'),
          disable_model_invocation: Boolean(s.disable_model_invocation),
          user_invocable: Boolean(s.user_invocable),
        })),
      )
    })
    client.command('skills', null, projectRoot)
    return off
  }, [client, projectRoot])

  return (
    <SpecView
      kind="skill"
      title="技能"
      hint="执行 /skills 返回的技能清单；可开关模型自动调用。"
      icon={<Sparkles size={18} />}
      specs={specs}
      client={client}
      projectRoot={projectRoot}
      onClose={onClose}
    />
  )
}

// M9.9 智能体视图：进入即执行 /agents，订阅 show_agents 渲染可用子 Agent。
export function AgentsPanel({ client, projectRoot, onClose }: Props): React.ReactElement {
  const [specs, setSpecs] = useState<Spec[] | null>(null)

  useEffect(() => {
    if (!client) return
    setSpecs(null)
    const off = client.onMessage('show_agents', (env) => {
      const arr = (env.payload['specs'] as Array<Record<string, unknown>>) ?? []
      setSpecs(
        arr.map((s) => ({
          name: String(s.name ?? '?'),
          description: String(s.description ?? ''),
          source: String(s.source ?? (s.builtin ? 'builtin' : 'user')),
          tools: (s.tools as string[] | null | undefined) ?? null,
          disallowed_tools: (s.disallowed_tools as string[] | undefined) ?? [],
          model: (s.model as string | null | undefined) ?? null,
          permission_mode: (s.permission_mode as string | null | undefined) ?? null,
          max_turns: (s.max_turns as number | null | undefined) ?? null,
          panel_height: (s.panel_height as number | undefined) ?? 15,
          builtin: Boolean(s.builtin),
          share_history: Boolean(s.share_history),
        })),
      )
    })
    client.command('agents', null, projectRoot)
    return off
  }, [client, projectRoot])

  return (
    <SpecView
      kind="agent"
      title="智能体"
      hint="点击卡片可编辑配置（内置为只读）。"
      icon={<Bot size={18} />}
      specs={specs}
      client={client}
      projectRoot={projectRoot}
      onClose={onClose}
    />
  )
}

function SpecView({
  kind,
  title,
  hint,
  icon,
  specs,
  client,
  projectRoot,
  onClose,
}: {
  kind: SpecKind
  title: string
  hint: string
  icon: React.ReactNode
  specs: Spec[] | null
  client: DaemonClient | null
  projectRoot: string
  onClose: () => void
}): React.ReactElement {
  const [editing, setEditing] = useState<Spec | null>(null)
  // M11.6 工具白名单选项：从后台真实注册表动态获取（/tools → show_tools），
  // 避免前端硬编码与后端实际注册工具不一致。
  const [toolNames, setToolNames] = useState<string[]>([])

  useEffect(() => {
    if (!client) return
    setToolNames([])
    const off = client.onMessage('show_tools', (env) => {
      const arr = (env.payload['tools'] as Array<Record<string, unknown>>) ?? []
      setToolNames(arr.map((t) => String(t.name ?? '')).filter(Boolean))
    })
    client.command('tools', null, projectRoot)
    return off
  }, [client, projectRoot])

  // 按来源分组：builtin / user / project。
  const groups = useMemo(() => {
    const order = ['builtin', 'user', 'project']
    const map: Record<string, Spec[]> = {}
    for (const s of specs ?? []) {
      const key = SOURCE_LABEL[s.source ?? 'user'] ?? '其他'
      ;(map[key] ??= []).push(s)
    }
    return order
      .map((k) => ({ label: SOURCE_LABEL[k] ?? k, items: map[SOURCE_LABEL[k]] ?? [] }))
      .filter((g) => g.items.length > 0)
  }, [specs])

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
          groups.map((g) => (
            <div key={g.label} className="wa-spec-group">
              <div className="wa-spec-group__label">{g.label}</div>
              <ul className="wa-specview__list">
                {g.items.map((s) => (
                  <SpecCard
                    key={s.name}
                    kind={kind}
                    spec={s}
                    client={client}
                    projectRoot={projectRoot}
                    onEdit={kind === 'agent' && !s.builtin ? () => setEditing(s) : undefined}
                  />
                ))}
              </ul>
            </div>
          ))
        )}
      </div>
      {kind === 'agent' && editing && client && (
        <AgentEditorModal
          spec={editing}
          client={client}
          projectRoot={projectRoot}
          toolNames={toolNames}
          onClose={() => setEditing(null)}
        />
      )}
    </section>
  )
}

function SpecCard({
  kind,
  spec,
  client,
  projectRoot,
  onEdit,
}: {
  kind: SpecKind
  spec: Spec
  client: DaemonClient | null
  projectRoot: string
  onEdit?: () => void
}): React.ReactElement {
  const [enabled, setEnabled] = useState(!spec.disable_model_invocation)
  const canToggle = kind === 'skill'

  return (
    <li className="wa-spec-card">
      <div className="wa-spec-card__main">
        <div className="wa-spec-card__name">
          {spec.name}
          <span className={`wa-spec-badge wa-spec-badge--${spec.source ?? 'user'}`}>
            {SOURCE_LABEL[spec.source ?? 'user'] ?? '其他'}
          </span>
          {kind === 'agent' && spec.builtin && (
            <span className="wa-spec-badge wa-spec-badge--builtin">只读</span>
          )}
        </div>
        <div className="wa-spec-card__desc">{spec.description || '（无描述）'}</div>
        {kind === 'agent' && spec.tools && (
          <div className="wa-spec-card__tools">工具：{spec.tools.join(', ')}</div>
        )}
      </div>
      <div className="wa-spec-card__actions">
        {canToggle && (
          <button
            type="button"
            className={`wa-toggle ${enabled ? 'wa-toggle--on' : ''}`}
            title={enabled ? '模型可自动调用（点击禁用）' : '仅手动 /name（点击启用）'}
            onClick={() => {
              const next = !enabled
              setEnabled(next)
              client?.setSkillEnabled(spec.name, next, projectRoot)
            }}
          >
            <span className="wa-toggle__knob" />
          </button>
        )}
        {kind === 'agent' && onEdit && (
          <button type="button" className="wa-spec-card__edit" title="编辑配置" onClick={onEdit}>
            <Pencil size={13} />
          </button>
        )}
      </div>
    </li>
  )
}

// 模型：subagent 模型走 OpenAI 兼容协议（settings.llm.{api_key,base_url,model}）。
// 这里只列默认 provider 的已知模型；自定义模型请改 .agent/settings.yaml 的 llm.model。
const MODEL_OPTIONS = ['inherit', 'deepseek-v4-flash']

// 权限模式：仅 plan 在 _resolve_security 有特殊映射（read-only + never 审批），
// 其余值当前不被识别，保留 inherit 作为「继承父会话」。
const PERMISSION_OPTIONS = ['inherit', 'plan']

function AgentEditorModal({
  spec,
  client,
  projectRoot,
  toolNames,
  onClose,
}: {
  spec: Spec
  client: DaemonClient
  projectRoot: string
  /** M11.6 后台真实注册工具名清单（show_tools 拉取），用于渲染白名单勾选。 */
  toolNames: string[]
  onClose: () => void
}): React.ReactElement {
  // tools: null = 继承全部；否则为白名单。
  const [inheritTools, setInheritTools] = useState<boolean>(spec.tools == null)
  const [selTools, setSelTools] = useState<Set<string>>(new Set(spec.tools ?? []))
  const [desc, setDesc] = useState(spec.description ?? '')
  const [model, setModel] = useState(spec.model ?? 'inherit')
  const [permission, setPermission] = useState(spec.permission_mode ?? 'inherit')
  const [maxTurns, setMaxTurns] = useState(String(spec.max_turns ?? ''))
  const [shareHistory, setShareHistory] = useState(Boolean(spec.share_history))

  const toggleTool = (t: string): void => {
    setSelTools((prev) => {
      const next = new Set(prev)
      if (next.has(t)) next.delete(t)
      else next.add(t)
      return next
    })
  }

  const submit = (): void => {
    const updates: Record<string, unknown> = {
      description: desc,
      model: model === 'inherit' ? null : model || null,
      permission_mode: permission === 'inherit' ? null : permission || null,
      max_turns: maxTurns ? Number(maxTurns) : null,
      share_history: shareHistory,
    }
    updates.tools = inheritTools ? null : Array.from(selTools)
    client.updateAgent(spec.name, updates, projectRoot)
    onClose()
  }

  return (
    <div className="wa-editor-overlay" onClick={onClose}>
      <div className="wa-editor-modal" onClick={(e) => e.stopPropagation()}>
        <div className="wa-editor-modal__head">
          <span className="wa-editor-modal__head-icon">
            <Bot size={16} />
          </span>
          <span className="wa-editor-modal__head-title">
            编辑智能体
            <span className="wa-editor-modal__head-sub">{spec.name}</span>
          </span>
          <IconButton icon={<X size={14} />} label="关闭" onClick={onClose} />
        </div>

        <div className="wa-editor-modal__body">
          {/* 基本信息 */}
          <section className="wa-editor-sec">
            <div className="wa-editor-sec__label">基本信息</div>
            <label className="wa-editor-field">
              <span>描述</span>
              <textarea
                value={desc}
                onChange={(e) => setDesc(e.target.value)}
                rows={2}
                placeholder="该智能体的职责说明"
              />
            </label>
          </section>

          {/* 能力 */}
          <section className="wa-editor-sec">
            <div className="wa-editor-sec__label">能力</div>
            <div className="wa-editor-field">
              <span className="wa-editor-field__label">工具白名单</span>
              <div className="wa-inherit-line">
                <label className="wa-ed-switch">
                  <input
                    type="checkbox"
                    checked={inheritTools}
                    onChange={(e) => setInheritTools(e.target.checked)}
                  />
                  <span className="wa-ed-switch__track">
                    <span className="wa-ed-switch__thumb" />
                  </span>
                  <span className="wa-ed-switch__text">继承父会话全部工具</span>
                </label>
              </div>
              {!inheritTools && (
                <div className="wa-tool-grid">
                  {toolNames.length === 0 ? (
                    <div className="wa-tool-grid__empty">加载工具清单…</div>
                  ) : (
                    toolNames.map((t) => (
                      <label key={t} className={`wa-check ${selTools.has(t) ? 'wa-check--on' : ''}`}>
                        <input
                          type="checkbox"
                          checked={selTools.has(t)}
                          onChange={() => toggleTool(t)}
                        />
                        <span className="wa-check__box">
                          <CheckIcon size={11} strokeWidth={3} />
                        </span>
                        <span className="wa-check__text">{t}</span>
                      </label>
                    ))
                  )}
                </div>
              )}
            </div>
          </section>

          {/* 执行 */}
          <section className="wa-editor-sec">
            <div className="wa-editor-sec__label">执行参数</div>
            <div className="wa-editor-grid">
              <label className="wa-editor-field">
                <span>模型</span>
                <span className="wa-select-wrap">
                  <select value={model} onChange={(e) => setModel(e.target.value)}>
                    {MODEL_OPTIONS.map((m) => (
                      <option key={m} value={m}>
                        {m === 'inherit' ? '继承父会话模型' : m}
                      </option>
                    ))}
                  </select>
                </span>
              </label>
              <label className="wa-editor-field">
                <span>权限模式</span>
                <span className="wa-select-wrap">
                  <select value={permission} onChange={(e) => setPermission(e.target.value)}>
                    {PERMISSION_OPTIONS.map((p) => (
                      <option key={p} value={p}>
                        {p === 'inherit' ? '继承父会话' : p}
                      </option>
                    ))}
                  </select>
                </span>
              </label>
            </div>
            <div className="wa-editor-grid">
              <label className="wa-editor-field">
                <span>最大轮数</span>
                <input
                  type="number"
                  min={1}
                  value={maxTurns}
                  onChange={(e) => setMaxTurns(e.target.value)}
                  placeholder="默认"
                />
              </label>
              <label className="wa-editor-field">
                <span>共享历史</span>
                <label className="wa-ed-switch">
                  <input
                    type="checkbox"
                    checked={shareHistory}
                    onChange={(e) => setShareHistory(e.target.checked)}
                  />
                  <span className="wa-ed-switch__track">
                    <span className="wa-ed-switch__thumb" />
                  </span>
                </label>
              </label>
            </div>
          </section>
        </div>

        <div className="wa-editor-modal__foot">
          <span className="wa-editor-modal__name">{spec.name}</span>
          <div className="wa-editor-modal__actions">
            <button type="button" className="wa-btn wa-btn--ghost" onClick={onClose}>
              取消
            </button>
            <button type="button" className="wa-btn wa-btn--primary" onClick={submit}>
              保存
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
