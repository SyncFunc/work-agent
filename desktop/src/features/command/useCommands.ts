// 命令注册表（对齐 M7.5 的 command 集）+ run（经 DaemonClient.command 发往 daemon）。
// daemon 处理后经 notify / show_skills / show_agents 反馈（见 useNotices）。

import { useCallback, useMemo } from 'react'
import { DaemonClient } from '../../protocol/client'

export interface CommandDef {
  name: string
  description: string
  /** 是否需要会话上下文（部分命令如 /resume /fork 依赖当前会话）。 */
  needsSession?: boolean
}

export const COMMANDS: CommandDef[] = [
  { name: 'context', description: '显示当前上下文用量' },
  { name: 'compact', description: '触发上下文压缩' },
  { name: 'plan', description: '进入/展示计划模式' },
  { name: 'skills', description: '列出可用 skills' },
  { name: 'agents', description: '列出可用子 agents' },
  { name: 'mode', description: '切换模式（plan/normal）' },
  { name: 'exec', description: '直接执行命令' },
  { name: 'approve', description: '审批待决工具调用' },
  { name: 'bg', description: '后台运行任务' },
  { name: 'sessions', description: '列出会话' },
  { name: 'resume', description: '恢复历史会话', needsSession: true },
  { name: 'fork', description: '派生当前会话分支', needsSession: true },
  { name: 'skill', description: '运行指定 skill' },
  { name: 'agent', description: '运行指定子 agent' },
  { name: 'help', description: '显示帮助' },
]

export interface UseCommands {
  commands: CommandDef[]
  run: (name: string, args?: string) => void
}

export function useCommands(client: DaemonClient | null): UseCommands {
  const run = useCallback(
    (name: string, args = '') => {
      client?.command(name, args.trim() ? args.trim() : null)
    },
    [client],
  )
  const commands = useMemo(() => COMMANDS, [])
  return { commands, run }
}
