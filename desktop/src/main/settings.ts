// 主进程侧：读写项目级 <project_root>/.agent/settings.yaml（fs + js-yaml）。
// 仅在主进程调用（渲染进程经 IPC 经 preload 的 agentApi 访问），与 agent/config/settings.py 字段一致。

import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { dump, load } from 'js-yaml'

export type SettingsValue = unknown

/** 深合并 patch 到 base（返回新对象，不修改入参）。数组整体替换。 */
export function mergeSettings(
  base: Record<string, unknown>,
  patch: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = { ...base }
  for (const [k, v] of Object.entries(patch)) {
    const cur = out[k]
    if (
      v !== null &&
      typeof v === 'object' &&
      !Array.isArray(v) &&
      cur !== null &&
      typeof cur === 'object' &&
      !Array.isArray(cur)
    ) {
      out[k] = mergeSettings(cur as Record<string, unknown>, v as Record<string, unknown>)
    } else {
      out[k] = v
    }
  }
  return out
}

export function settingsPath(projectRoot: string): string {
  return join(projectRoot, '.agent', 'settings.yaml')
}

export function readSettings(projectRoot: string): Record<string, unknown> {
  const p = settingsPath(projectRoot)
  if (!existsSync(p)) return {}
  try {
    const doc = load(readFileSync(p, 'utf-8'))
    return doc && typeof doc === 'object' ? (doc as Record<string, unknown>) : {}
  } catch {
    return {}
  }
}

/** 合并 patch 后写回 YAML；返回合并后的完整配置。 */
export function writeSettings(
  projectRoot: string,
  patch: Record<string, unknown>,
): Record<string, unknown> {
  const merged = mergeSettings(readSettings(projectRoot), patch)
  const p = settingsPath(projectRoot)
  mkdirSync(join(projectRoot, '.agent'), { recursive: true })
  writeFileSync(p, dump(merged, { lineWidth: 120 }), 'utf-8')
  return merged
}
