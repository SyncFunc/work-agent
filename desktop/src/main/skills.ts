// 主进程侧：列出可用的技能（项目级 / 用户级），供命令候选框展示。
// 与 agent/skills/loader.py 的发现目录保持一致：
//   项目级 <project_root>/.agent/skills/<name>/SKILL.md
//   用户级 <user_config_dir>/skills/<name>/SKILL.md

import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { env } from 'node:process'
import { load } from 'js-yaml'

export interface SkillInfo {
  /** 技能标识（用于 /<name> 调用）。 */
  name: string
  /** 一句话说明。 */
  description: string
  /** 适用场景。 */
  when_to_use: string
}

function userConfigDir(): string {
  const override = env['AGENT_USER_CONFIG_DIR']
  return override && override.trim() ? override.trim() : join(homedir(), '.agent')
}

function scanDir(dir: string, out: Map<string, SkillInfo>): void {
  if (!existsSync(dir)) return
  let entries: string[]
  try {
    entries = readdirSync(dir, { withFileTypes: true })
      .filter((e) => e.isDirectory())
      .map((e) => e.name)
  } catch {
    return
  }
  for (const name of entries) {
    const skillMd = join(dir, name, 'SKILL.md')
    if (!existsSync(skillMd)) continue
    let text = ''
    try {
      text = readFileSync(skillMd, 'utf-8')
    } catch {
      continue
    }
    const m = /^---\s*\r?\n([\s\S]*?)\r?\n---\s*\r?\n?/.exec(text)
    if (!m) continue
    let meta: Record<string, unknown> = {}
    try {
      meta = (load(m[1]) as Record<string, unknown>) ?? {}
    } catch {
      continue
    }
    const id = String(meta['name'] ?? name)
    out.set(id, {
      name: id,
      description: String(meta['description'] ?? ''),
      when_to_use: String(meta['when_to_use'] ?? ''),
    })
  }
}

/** 列出全部可用技能（项目级优先于用户级）。 */
export function listSkills(projectRoot: string): SkillInfo[] {
  const out = new Map<string, SkillInfo>()
  scanDir(join(projectRoot, '.agent', 'skills'), out)
  scanDir(join(userConfigDir(), 'skills'), out)
  return [...out.values()]
}
