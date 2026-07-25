import { spawn } from 'node:child_process'

/**
 * 定位 Python 解释器：依次尝试 AGENT_PYTHON 环境变量 → `python` → `python3`。
 * 用 `python --version` 探测是否可用（捕获 spawn 失败与非零退出码）。
 */
export async function locatePython(): Promise<string> {
  const candidates: string[] = []
  if (process.env.AGENT_PYTHON) {
    candidates.push(process.env.AGENT_PYTHON)
  }
  candidates.push('python', 'python3')

  for (const cmd of candidates) {
    if (await probe(cmd)) {
      return cmd
    }
  }
  throw new Error(
    '未找到可用的 Python 解释器。请安装 Python 3.12+ 并确保在 PATH 中可用，' +
      '或设置 AGENT_PYTHON 环境变量指向解释器路径。',
  )
}

function probe(cmd: string): Promise<boolean> {
  return new Promise((resolve) => {
    try {
      const child = spawn(cmd, ['--version'], { stdio: ['ignore', 'pipe', 'pipe'] })
      child.on('error', () => resolve(false))
      child.on('close', (code) => resolve(code === 0))
    } catch {
      resolve(false)
    }
  })
}
