// 渲染进程侧设置读写封装（经 contextBridge 的 agentApi → 主进程 fs）。
// 字段、分组、默认值与 agent/config/settings.py 的 Settings 严格对齐，
// 使设置面板可覆盖全部可配置项，且默认回退值与 Python 端一致。

export interface SettingsShape {
  llm?: { api_key?: string; base_url?: string; model?: string }
  loop?: {
    max_iterations?: number
    max_tool_concurrency?: number
    max_repeat_calls?: number
    max_tool_output_chars?: number
  }
  sandbox?: { mode?: string; profile?: string }
  approval?: {
    mode?: string
    noninteractive_default?: string
    exec_policy?: string[]
    elevated_sandbox_profile?: string
  }
  plan?: { mode?: boolean; file?: string }
  clarify?: { enabled?: boolean; max_rounds?: number; hint_min_chars?: number }
  bash?: { shell?: string }
  obs?: { enabled?: boolean; db_path?: string; sessions_db_path?: string }
  resilience?: {
    enabled?: boolean
    rate_limit?: {
      llm_max_calls?: number
      llm_window_seconds?: number
      sandbox_max_calls?: number
      sandbox_window_seconds?: number
    }
    circuit_breaker?: {
      llm_failure_threshold?: number
      llm_recovery_timeout?: number
      sandbox_failure_threshold?: number
      sandbox_recovery_timeout?: number
    }
    fallback?: { llm_strategy?: string; sandbox_strategy?: string }
  }
  context?: {
    context_window?: number
    max_output_tokens?: number
    compact_buffer?: number
    microcompact_keep_recent?: number
    microcompact_enabled?: boolean
    auto_compact_enabled?: boolean
    session_memory_enabled?: boolean
    session_memory_dir?: string
    session_memory_min_message_tokens?: number
    session_memory_min_tokens_between?: number
    session_memory_tool_calls_between?: number
    agents_md_path?: string
    agents_md_enabled?: boolean
    event_stream_maxlen?: number
  }
  skills?: { enabled?: boolean; dirs?: string[] }
  subagents?: { enabled?: boolean; max_depth?: number; auto_allow?: boolean }
  daemon?: { host?: string; port?: number; health_port?: number; token?: string }
  ui?: { theme?: string }
}

// 与 settings.py 默认值严格一致（字段名、取值、多选枚举）。
export const DEFAULT_SETTINGS: SettingsShape = {
  llm: { api_key: '', base_url: 'https://api.deepseek.com', model: 'deepseek-v4-flash' },
  loop: {
    max_iterations: 25,
    max_tool_concurrency: 5,
    max_repeat_calls: 3,
    max_tool_output_chars: 20000,
  },
  sandbox: { mode: 'local', profile: 'workspace-write' },
  approval: {
    mode: 'on-request',
    noninteractive_default: 'allow',
    exec_policy: [],
    elevated_sandbox_profile: 'danger-full',
  },
  plan: { mode: false, file: '.agent/plan.md' },
  clarify: { enabled: true, max_rounds: 2, hint_min_chars: 0 },
  bash: { shell: '' },
  obs: { enabled: true, db_path: '.agent/traces.db', sessions_db_path: '.agent/sessions/sessions.db' },
  resilience: {
    enabled: true,
    rate_limit: {
      llm_max_calls: 60,
      llm_window_seconds: 60,
      sandbox_max_calls: 120,
      sandbox_window_seconds: 60,
    },
    circuit_breaker: {
      llm_failure_threshold: 5,
      llm_recovery_timeout: 30,
      sandbox_failure_threshold: 10,
      sandbox_recovery_timeout: 60,
    },
    fallback: { llm_strategy: 'retry', sandbox_strategy: 'fail_fast' },
  },
  context: {
    context_window: 200000,
    max_output_tokens: 20000,
    compact_buffer: 13000,
    microcompact_keep_recent: 5,
    microcompact_enabled: true,
    auto_compact_enabled: true,
    session_memory_enabled: true,
    session_memory_dir: '.agent/sessions',
    session_memory_min_message_tokens: 10000,
    session_memory_min_tokens_between: 5000,
    session_memory_tool_calls_between: 3,
    agents_md_path: 'AGENTS.md',
    agents_md_enabled: true,
    event_stream_maxlen: 4000,
  },
  skills: { enabled: true, dirs: [] },
  subagents: { enabled: true, max_depth: 5, auto_allow: false },
  daemon: { host: '127.0.0.1', port: 18789, health_port: 18790, token: '' },
  ui: { theme: 'dark' },
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

// 深合并：patch 覆盖 base；嵌套对象递归，数组/标量直接替换。
function deepMerge(base: unknown, patch: unknown): unknown {
  if (!isObj(base) || !isObj(patch)) return patch !== undefined ? patch : base
  const out: Record<string, unknown> = { ...base }
  for (const k of Object.keys(patch)) {
    const bv = base[k]
    const pv = patch[k]
    if (pv === undefined) continue
    if (isObj(bv) && isObj(pv)) out[k] = deepMerge(bv, pv)
    else out[k] = pv
  }
  return out
}

export async function loadSettings(projectRoot: string): Promise<SettingsShape> {
  const raw = (await window.agentApi.readSettings(projectRoot)) as SettingsShape | undefined
  return deepMerge(DEFAULT_SETTINGS, raw ?? {}) as SettingsShape
}

export async function saveSettings(
  projectRoot: string,
  patch: SettingsShape,
): Promise<SettingsShape> {
  const merged = await window.agentApi.writeSettings(projectRoot, patch as Record<string, unknown>)
  return deepMerge(DEFAULT_SETTINGS, merged ?? {}) as SettingsShape
}

/** 读取用户级与项目级配置（各自与默认值合并后的完整结构）。 */
export async function loadSettingsScoped(
  projectRoot: string,
): Promise<{ user: SettingsShape; project: SettingsShape }> {
  const { user, project } = await window.agentApi.readSettingsScoped(projectRoot)
  return {
    user: deepMerge(DEFAULT_SETTINGS, user ?? {}) as SettingsShape,
    project: deepMerge(DEFAULT_SETTINGS, project ?? {}) as SettingsShape,
  }
}

/** 将 patch 写入指定作用域（user/project）并返回该作用域合并默认值后的完整结构。 */
export async function saveSettingsScoped(
  projectRoot: string,
  patch: Record<string, unknown>,
  scope: 'user' | 'project',
): Promise<SettingsShape> {
  const merged = await window.agentApi.writeSettingsScoped(projectRoot, patch, scope)
  return deepMerge(DEFAULT_SETTINGS, merged ?? {}) as SettingsShape
}

export interface SkillInfo {
  name: string
  description: string
  when_to_use: string
}

/** 列出可用技能（项目级优先于用户级）。 */
export async function listSkills(projectRoot: string): Promise<SkillInfo[]> {
  return window.agentApi.listSkills(projectRoot)
}

const THEME_KEY = 'workagent.theme'

export type Theme = 'light' | 'dark'

export function loadTheme(): Theme {
  const t = localStorage.getItem(THEME_KEY)
  return t === 'dark' ? 'dark' : 'light'
}

export function applyTheme(theme: Theme): void {
  document.body.dataset.theme = theme
  localStorage.setItem(THEME_KEY, theme)
}
