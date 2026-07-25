// 渲染进程侧设置读写封装（经 contextBridge 的 agentApi → 主进程 fs）。
// 字段与 agent/config/settings.py 对齐（llm/plan/clarify/ui/sandbox/approval）。

export interface SettingsShape {
  llm?: { api_key?: string; base_url?: string; model?: string }
  plan?: { mode?: string }
  clarify?: { enabled?: boolean }
  ui?: { theme?: string }
  sandbox?: { profile?: string }
  approval?: { mode?: string }
  context?: { context_window?: number }
}

export async function loadSettings(projectRoot: string): Promise<SettingsShape> {
  const raw = await window.agentApi.readSettings(projectRoot)
  return raw as SettingsShape
}

export async function saveSettings(
  projectRoot: string,
  patch: SettingsShape,
): Promise<SettingsShape> {
  const merged = await window.agentApi.writeSettings(projectRoot, patch as Record<string, unknown>)
  return merged as SettingsShape
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
