// 主进程侧：读写 <project_root>/.agent/settings.yaml（项目级）与
// <user_config_dir>/settings.yaml（用户级）（fs + js-yaml）。
// 仅在主进程调用（渲染进程经 IPC 经 preload 的 agentApi 访问），与 agent/config/settings.py 字段一致。
// 优先级：项目级 > 用户级（与 settings.py 一致）。

import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { env } from 'node:process'
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

function userConfigDir(): string {
  const override = env['AGENT_USER_CONFIG_DIR']
  return override && override.trim() ? override.trim() : join(homedir(), '.agent')
}

function userSettingsPath(): string {
  return join(userConfigDir(), 'settings.yaml')
}

function readYaml(p: string): Record<string, unknown> {
  if (!existsSync(p)) return {}
  try {
    const doc = load(readFileSync(p, 'utf-8'))
    return doc && typeof doc === 'object' ? (doc as Record<string, unknown>) : {}
  } catch {
    return {}
  }
}

export function readSettings(projectRoot: string): Record<string, unknown> {
  return readYaml(settingsPath(projectRoot))
}

/** 读取用户级与项目级两份配置（各自原始内容，未与默认值合并）。 */
export function readSettingsScoped(projectRoot: string): {
  user: Record<string, unknown>
  project: Record<string, unknown>
} {
  return {
    user: readYaml(userSettingsPath()),
    project: readYaml(settingsPath(projectRoot)),
  }
}

export type SettingsScope = 'user' | 'project'

export function writeSettings(
  projectRoot: string,
  patch: Record<string, unknown>,
): Record<string, unknown> {
  return writeSettingsScoped(projectRoot, patch, 'project')
}

/**
 * 将 patch 应用到 base，返回新对象。与 mergeSettings 不同的是：
 * - 叶子值为空（'' / null / undefined / 0）时从结果中删除该键，
 *   使清空字段能正确回落到更低优先级作用域（用户级 / 内置默认）。
 * - 布尔值 false 视为有效值，保留。
 * - 数组整体替换。
 */
function applyPatch(
  base: Record<string, unknown>,
  patch: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = structuredCloneSafe(base)
  for (const [k, v] of Object.entries(patch)) {
    if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
      out[k] = applyPatch((out[k] as Record<string, unknown>) ?? {}, v as Record<string, unknown>)
    } else if (v === '' || v === null || v === undefined || v === 0) {
      delete out[k]
    } else {
      out[k] = v
    }
  }
  return out
}

function structuredCloneSafe(v: unknown): Record<string, unknown> {
  return JSON.parse(JSON.stringify(v ?? {})) as Record<string, unknown>
}

/** 将 patch 应用（含空值删除）到指定作用域的配置文件并返回结果。 */
export function writeSettingsScoped(
  projectRoot: string,
  patch: Record<string, unknown>,
  scope: SettingsScope,
): Record<string, unknown> {
  const p = scope === 'user' ? userSettingsPath() : settingsPath(projectRoot)
  const next = applyPatch(readYaml(p), patch)
  mkdirSync(dirname(p), { recursive: true })
  writeFileSync(p, dump(next, { lineWidth: 120 }), 'utf-8')
  return next
}
