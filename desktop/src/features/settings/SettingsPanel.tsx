import { useEffect, useState } from 'react'
import { Seg } from '../../components'
import { applyTheme, DEFAULT_SETTINGS, loadSettingsScoped, saveSettingsScoped, type Theme } from './settingsApi'
import './SettingsPanel.css'

type Scope = 'project' | 'user'

type FieldType = 'text' | 'password' | 'number' | 'select' | 'toggle' | 'theme' | 'list'

interface Opt {
  value: string
  label: string
}

interface FieldDef {
  key: string
  label: string
  type: FieldType
  options?: Opt[]
  placeholder?: string
  /** 项目级为空时可回退到用户级展示。 */
  inheritable?: boolean
  help?: string
}

interface GroupDef {
  id: string
  title: string
  desc?: string
  fields: FieldDef[]
}

// 主题切换器（界面主题：浅色 / 深色，带图标）。
function SunIcon(): React.ReactElement {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  )
}

function MoonIcon(): React.ReactElement {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z" />
    </svg>
  )
}

const THEME_OPTS: { value: Theme; label: string; icon: React.ReactElement }[] = [
  { value: 'light', label: '浅色', icon: <SunIcon /> },
  { value: 'dark', label: '深色', icon: <MoonIcon /> },
]

function ThemeSwitcher({ value, onChange }: { value: string; onChange: (v: Theme) => void }): React.ReactElement {
  return (
    <div className="wa-theme-switch" role="radiogroup" aria-label="界面主题">
      {THEME_OPTS.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={o.value === value}
          className={`wa-theme-switch__opt${o.value === value ? ' is-active' : ''}`}
          onClick={() => onChange(o.value)}
        >
          <span className="wa-theme-switch__icon" aria-hidden>
            {o.icon}
          </span>
          <span className="wa-theme-switch__label">{o.label}</span>
        </button>
      ))}
    </div>
  )
}

// ---- 多选 / 模式字段的合法枚举（与 agent 源码严格一致） ----

const SANDBOX_MODE: Opt[] = [
  { value: 'local', label: 'local（本地进程执行）' },
  { value: 'docker', label: 'docker（Docker 隔离）' },
  { value: 'external', label: 'external（外部执行器）' },
]

const SANDBOX_PROFILE: Opt[] = [
  { value: 'read-only', label: 'read-only（只读，禁止写入/联网）' },
  { value: 'workspace-write', label: 'workspace-write（仅工作区可写，断网）' },
  { value: 'danger-full', label: 'danger-full（完全访问，放行网络）' },
]

const APPROVAL_MODE: Opt[] = [
  { value: 'on-request', label: 'on-request（默认放行，模型显式请求才询问）' },
  { value: 'unless-trusted', label: 'unless-trusted（exec/edit 每步问，策略命中免审）' },
  { value: 'on-failure', label: 'on-failure（先执行，失败才问）' },
  { value: 'never', label: 'never（永不请求审批，权限不足直接失败）' },
]

const YESNO: Opt[] = [
  { value: 'allow', label: 'allow（非交互时放行）' },
  { value: 'deny', label: 'deny（非交互时拒绝）' },
]

const FALLBACK_STRATEGY: Opt[] = [
  { value: 'fail_fast', label: 'fail_fast（直接失败，不降级）' },
  { value: 'retry', label: 'retry（重试）' },
  { value: 'cache', label: 'cache（返回缓存）' },
  { value: 'mock', label: 'mock（返回模拟结果）' },
]

// ---- 分组（与 settings.py 的 Settings 子模型一一对应） ----

const GROUPS: GroupDef[] = [
  {
    id: 'llm',
    title: 'LLM / 模型',
    desc: '模型接入与密钥',
    fields: [
      { key: 'llm.api_key', label: 'API Key', type: 'password', placeholder: 'sk-...', inheritable: true },
      { key: 'llm.base_url', label: 'API Base URL', type: 'text', placeholder: 'https://api.deepseek.com', inheritable: true },
      { key: 'llm.model', label: '模型', type: 'text', placeholder: 'deepseek-v4-flash', inheritable: true },
    ],
  },
  {
    id: 'loop',
    title: '循环 (Loop)',
    desc: 'ReAct 循环上限',
    fields: [
      { key: 'loop.max_iterations', label: '最大迭代次数', type: 'number', inheritable: true },
      { key: 'loop.max_tool_concurrency', label: '最大工具并发', type: 'number', inheritable: true },
      { key: 'loop.max_repeat_calls', label: '最大重复调用', type: 'number', inheritable: true },
      { key: 'loop.max_tool_output_chars', label: '工具输出最大字符', type: 'number', inheritable: true },
    ],
  },
  {
    id: 'sandbox',
    title: '沙箱 (Sandbox)',
    desc: '命令执行隔离',
    fields: [
      { key: 'sandbox.mode', label: '执行模式', type: 'select', options: SANDBOX_MODE, inheritable: true },
      { key: 'sandbox.profile', label: '沙箱档位', type: 'select', options: SANDBOX_PROFILE, inheritable: true },
    ],
  },
  {
    id: 'approval',
    title: '审批 (Approval)',
    desc: '工具调用审批策略',
    fields: [
      { key: 'approval.mode', label: '审批模式', type: 'select', options: APPROVAL_MODE, inheritable: true },
      { key: 'approval.noninteractive_default', label: '非交互默认', type: 'select', options: YESNO, inheritable: true },
      {
        key: 'approval.exec_policy',
        label: '免审命令',
        type: 'list',
        inheritable: true,
        help: '逗号分隔；命中这些命令时免审（unless-trusted 模式生效）',
      },
      { key: 'approval.elevated_sandbox_profile', label: '提权沙箱档位', type: 'select', options: SANDBOX_PROFILE, inheritable: true },
    ],
  },
  {
    id: 'plan',
    title: '计划模式 (Plan)',
    fields: [
      { key: 'plan.mode', label: '启用计划模式', type: 'toggle', inheritable: true },
      { key: 'plan.file', label: '计划文件路径', type: 'text', inheritable: true },
    ],
  },
  {
    id: 'clarify',
    title: '意图澄清 (Clarify)',
    fields: [
      { key: 'clarify.enabled', label: '启用意图澄清', type: 'toggle', inheritable: true },
      { key: 'clarify.max_rounds', label: '最大澄清轮数', type: 'number', inheritable: true },
      { key: 'clarify.hint_min_chars', label: '提示最小字符', type: 'number', inheritable: true },
    ],
  },
  {
    id: 'bash',
    title: 'Shell',
    fields: [{ key: 'bash.shell', label: 'Shell 命令', type: 'text', placeholder: '默认跟随系统（如 bash / pwsh）' }],
  },
  {
    id: 'obs',
    title: '可观测 (Observability)',
    desc: 'Trace / 会话存储',
    fields: [
      { key: 'obs.enabled', label: '启用可观测', type: 'toggle' },
      { key: 'obs.db_path', label: 'Trace 数据库路径', type: 'text' },
      { key: 'obs.sessions_db_path', label: '会话数据库路径', type: 'text' },
    ],
  },
  {
    id: 'resilience',
    title: '韧性层 (Resilience)',
    desc: '限流 / 熔断 / 降级',
    fields: [
      { key: 'resilience.enabled', label: '启用韧性层', type: 'toggle' },
      { key: 'resilience.rate_limit.llm_max_calls', label: 'LLM 限流·单窗口最大调用', type: 'number' },
      { key: 'resilience.rate_limit.llm_window_seconds', label: 'LLM 限流·窗口秒', type: 'number' },
      { key: 'resilience.rate_limit.sandbox_max_calls', label: '沙箱限流·单窗口最大调用', type: 'number' },
      { key: 'resilience.rate_limit.sandbox_window_seconds', label: '沙箱限流·窗口秒', type: 'number' },
      { key: 'resilience.circuit_breaker.llm_failure_threshold', label: 'LLM 熔断·失败阈值', type: 'number' },
      { key: 'resilience.circuit_breaker.llm_recovery_timeout', label: 'LLM 熔断·恢复超时(秒)', type: 'number' },
      { key: 'resilience.circuit_breaker.sandbox_failure_threshold', label: '沙箱熔断·失败阈值', type: 'number' },
      { key: 'resilience.circuit_breaker.sandbox_recovery_timeout', label: '沙箱熔断·恢复超时(秒)', type: 'number' },
      { key: 'resilience.fallback.llm_strategy', label: 'LLM 降级策略', type: 'select', options: FALLBACK_STRATEGY },
      { key: 'resilience.fallback.sandbox_strategy', label: '沙箱降级策略', type: 'select', options: FALLBACK_STRATEGY },
    ],
  },
  {
    id: 'context',
    title: '上下文与记忆 (Context)',
    desc: '压缩 / 会话记忆',
    fields: [
      { key: 'context.context_window', label: '上下文窗口 (tokens)', type: 'number', inheritable: true },
      { key: 'context.max_output_tokens', label: '最大输出 tokens', type: 'number', inheritable: true },
      { key: 'context.compact_buffer', label: '压缩缓冲 tokens', type: 'number', inheritable: true },
      { key: 'context.microcompact_keep_recent', label: '微压缩·保留最近条数', type: 'number', inheritable: true },
      { key: 'context.microcompact_enabled', label: '启用微压缩', type: 'toggle', inheritable: true },
      { key: 'context.auto_compact_enabled', label: '启用自动压缩', type: 'toggle', inheritable: true },
      { key: 'context.session_memory_enabled', label: '启用会话记忆', type: 'toggle', inheritable: true },
      { key: 'context.session_memory_dir', label: '会话记忆目录', type: 'text', inheritable: true },
      { key: 'context.session_memory_min_message_tokens', label: '会话记忆·首次触发 tokens', type: 'number', inheritable: true },
      { key: 'context.session_memory_min_tokens_between', label: '会话记忆·最小增量 tokens', type: 'number', inheritable: true },
      { key: 'context.session_memory_tool_calls_between', label: '会话记忆·最小 tool call 数', type: 'number', inheritable: true },
      { key: 'context.agents_md_path', label: 'AGENTS.md 路径', type: 'text', inheritable: true },
      { key: 'context.agents_md_enabled', label: '启用 AGENTS.md', type: 'toggle', inheritable: true },
      { key: 'context.event_stream_maxlen', label: '事件流内存上限', type: 'number', inheritable: true },
    ],
  },
  {
    id: 'skills',
    title: '技能 (Skills)',
    fields: [
      { key: 'skills.enabled', label: '启用技能', type: 'toggle' },
      {
        key: 'skills.dirs',
        label: '额外技能目录',
        type: 'list',
        help: '逗号分隔；除项目级 .agent/skills、用户级 ~/.agent/skills 外的目录',
      },
    ],
  },
  {
    id: 'subagents',
    title: '子 Agent (Subagents)',
    fields: [
      { key: 'subagents.enabled', label: '启用子 Agent', type: 'toggle' },
      { key: 'subagents.max_depth', label: '最大嵌套深度', type: 'number' },
      { key: 'subagents.auto_allow', label: '自动放行子 Agent', type: 'toggle' },
    ],
  },
  {
    id: 'daemon',
    title: '守护进程 (Daemon)',
    desc: 'agentrunner 网络配置',
    fields: [
      { key: 'daemon.host', label: '绑定地址', type: 'text' },
      { key: 'daemon.port', label: 'WebSocket 端口', type: 'number' },
      { key: 'daemon.health_port', label: '健康检查端口', type: 'number' },
      { key: 'daemon.token', label: '鉴权 Token', type: 'password', placeholder: '空 = 不鉴权' },
    ],
  },
  {
    id: 'ui',
    title: '界面 (UI)',
    desc: '前端主题（独立 CSS 主题，不影响 Python Textual 主题）',
    fields: [{ key: 'ui.theme', label: '界面主题', type: 'theme' }],
  },
]

const ALL_FIELDS: FieldDef[] = GROUPS.flatMap((g) => g.fields)

const SCOPE_OPTS: { value: Scope; label: string }[] = [
  { value: 'project', label: '项目级' },
  { value: 'user', label: '用户级' },
]

function getRaw(obj: Record<string, unknown>, key: string): unknown {
  return key.split('.').reduce<unknown>((o, k) => {
    if (o && typeof o === 'object') return (o as Record<string, unknown>)[k]
    return undefined
  }, obj)
}

function setPath(obj: Record<string, unknown>, key: string, val: unknown): Record<string, unknown> {
  const keys = key.split('.')
  let cur = obj
  for (let i = 0; i < keys.length - 1; i++) {
    const k = keys[i]
    if (typeof cur[k] !== 'object' || cur[k] === null) cur[k] = {}
    cur = cur[k] as Record<string, unknown>
  }
  cur[keys[keys.length - 1]] = val
  return obj
}

type FormState = Record<string, unknown>

function isEmpty(v: unknown): boolean {
  return v === undefined || v === null || v === '' || (typeof v === 'number' && Number.isNaN(v))
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (typeof a !== typeof b) return false
  if (a && typeof a === 'object') {
    const ka = Object.keys(a as Record<string, unknown>)
    const kb = Object.keys(b as Record<string, unknown>)
    if (ka.length !== kb.length) return false
    return ka.every((k) => deepEqual((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]))
  }
  return false
}

interface Props {
  projectRoot: string
  onClose: () => void
  onToast: (kind: 'info' | 'success' | 'error', text: string) => void
}

export function SettingsPanel({ projectRoot, onClose, onToast }: Props): React.ReactElement {
  const [scope, setScope] = useState<Scope>('project')
  const [user, setUser] = useState<FormState>(DEFAULT_SETTINGS as unknown as FormState)
  const [project, setProject] = useState<FormState>(DEFAULT_SETTINGS as unknown as FormState)
  const [form, setForm] = useState<FormState>(DEFAULT_SETTINGS as unknown as FormState)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)

  const clone = (o: FormState): FormState => structuredClone(o)

  useEffect(() => {
    let alive = true
    void loadSettingsScoped(projectRoot).then((scoped) => {
      if (!alive) return
      setUser(scoped.user as unknown as FormState)
      setProject(scoped.project as unknown as FormState)
      setScope('project')
      setForm(clone(scoped.project as unknown as FormState))
      setDirty(false)
      setSaving(false)
    })
    return () => {
      alive = false
    }
  }, [projectRoot])

  const handleScope = (s: Scope): void => {
    if (s === scope) return
    setScope(s)
    setForm(clone(s === 'project' ? project : user))
    setDirty(false)
  }

  const handleReset = (): void => {
    setForm(clone(scope === 'project' ? project : user))
    setDirty(false)
  }

  const setField = (key: string, val: unknown): void => {
    setForm((prev) => setPath(structuredClone(prev) as Record<string, unknown>, key, val) as FormState)
    setDirty(true)
  }

  const handleSave = async (): Promise<void> => {
    if (saving) return
    const baseline = scope === 'project' ? project : user
    const patch: Record<string, unknown> = {}
    for (const f of ALL_FIELDS) {
      const v = getRaw(form, f.key)
      const b = getRaw(baseline, f.key)
      if (f.type === 'toggle') {
        if (Boolean(v) !== Boolean(b)) setPath(patch, f.key, Boolean(v))
      } else if (f.type === 'number') {
        // 数字清空不写入（避免把必填整数字段写成空串导致解析失败）。
        if (!isEmpty(v) && Number(v) !== Number(b)) setPath(patch, f.key, Number(v))
      } else if (!deepEqual(v, b)) {
        setPath(patch, f.key, isEmpty(v) ? '' : v)
      }
    }
    if (Object.keys(patch).length === 0) {
      onToast('info', '没有改动')
      return
    }
    setSaving(true)
    try {
      await saveSettingsScoped(projectRoot, patch, scope)
      const scoped = await loadSettingsScoped(projectRoot)
      setUser(scoped.user as unknown as FormState)
      setProject(scoped.project as unknown as FormState)
      setForm(clone(scope === 'project' ? (scoped.project as unknown as FormState) : (scoped.user as unknown as FormState)))
      setDirty(false)
      onToast('success', `已保存到${scope === 'user' ? '用户级' : '项目级'}设置`)
    } catch (e) {
      onToast('error', `保存失败：${(e as Error).message}`)
    } finally {
      setSaving(false)
    }
  }

  const renderControl = (f: FieldDef): React.ReactNode => {
    const value = getRaw(form, f.key)
    if (f.type === 'toggle') {
      const on = Boolean(value)
      return (
        <button
          type="button"
          className={`wa-switch${on ? ' is-on' : ''}`}
          role="switch"
          aria-checked={on}
          onClick={() => setField(f.key, !on)}
        >
          <span className="wa-switch__knob" />
        </button>
      )
    }
    if (f.type === 'select') {
      const opts = [...(f.options ?? [])]
      const cur = String(value ?? '')
      if (cur && !opts.some((o) => o.value === cur)) opts.push({ value: cur, label: `${cur}（当前值）` })
      return (
        <select
          className="wa-input wa-select"
          value={cur}
          onChange={(e) => setField(f.key, e.target.value)}
        >
          <option value="">（默认）</option>
          {opts.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      )
    }
    if (f.type === 'list') {
      const arr = Array.isArray(value) ? (value as string[]) : []
      return (
        <input
          className="wa-input"
          value={arr.join(', ')}
          placeholder={f.placeholder ?? '逗号分隔，如 a, b, c'}
          onChange={(e) => setField(f.key, e.target.value.split(',').map((s) => s.trim()).filter(Boolean))}
        />
      )
    }
    if (f.type === 'theme') {
      return (
        <ThemeSwitcher
          value={String(value ?? 'dark')}
          onChange={(v) => {
            setField(f.key, v)
            applyTheme(v)
          }}
        />
      )
    }
    const type = f.type === 'password' ? 'password' : f.type === 'number' ? 'number' : 'text'
    const inherited = f.inheritable === true && scope === 'project' && isEmpty(value)
    const userVal = getRaw(user, f.key)
    const ph = inherited && !isEmpty(userVal) ? `继承自用户级：${String(userVal)}` : (f.placeholder ?? '')
    return (
      <input
        className="wa-input"
        type={type}
        value={String(value ?? '')}
        placeholder={ph}
        onChange={(e) =>
          setField(f.key, f.type === 'number' ? (e.target.value === '' ? '' : Number(e.target.value)) : e.target.value)
        }
      />
    )
  }

  return (
    <div className="wa-settings">
      <div className="wa-settings__header">
        <button type="button" className="wa-settings__back" onClick={onClose} title="返回">
          <span aria-hidden>←</span> 返回
        </button>
        <div className="wa-settings__title">设置</div>
        <div className="wa-settings__status">
          {scope === 'project' ? '项目级（覆盖用户级，为空回退用户级）' : '用户级（全局默认）'}
        </div>
      </div>

      <div className="wa-settings__scopebar">
        <span className="wa-settings__scope-label">编辑作用域</span>
        <Seg
          options={SCOPE_OPTS.map((o) => o.label)}
          value={SCOPE_OPTS.find((o) => o.value === scope)!.label}
          onChange={(label) => handleScope(SCOPE_OPTS.find((o) => o.label === label)!.value)}
        />
      </div>

      <div className="wa-settings__body">
        {GROUPS.map((g) => (
          <section key={g.id} className="wa-settings__group">
            <div className="wa-settings__group-head">
              <h3>{g.title}</h3>
              {g.desc ? <p>{g.desc}</p> : null}
            </div>
            <div className="wa-settings__grid">
              {g.fields.map((f) => {
                const value = getRaw(form, f.key)
                const inherited = f.inheritable === true && scope === 'project' && isEmpty(value)
                return (
                  <div key={f.key} className={`wa-settings__field${inherited ? ' is-inherited' : ''}`}>
                    <div className="wa-settings__label">
                      <span className="wa-settings__label-text">{f.label}</span>
                      {f.help ? <span className="wa-settings__help">{f.help}</span> : null}
                      {inherited ? <span className="wa-settings__inherit-badge">继承自用户级</span> : null}
                    </div>
                    {renderControl(f)}
                  </div>
                )
              })}
            </div>
          </section>
        ))}

        <div className="wa-settings__actions">
          <button type="button" className="wa-btn wa-btn--ghost" onClick={handleReset} disabled={!dirty}>
            重置改动
          </button>
          <button
            type="button"
            className="wa-btn wa-btn--primary"
            onClick={() => void handleSave()}
            disabled={saving || !dirty}
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}
