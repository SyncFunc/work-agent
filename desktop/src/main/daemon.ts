import { spawn, type ChildProcess } from 'node:child_process'
import http from 'node:http'
import { locatePython } from './python'
import type { DaemonConfig } from '../shared/daemon-config'

const DEFAULT_HOST = '127.0.0.1'
const DEFAULT_HEALTH_PORT = 18790
const START_TIMEOUT_MS = 15000
const HEALTH_POLL_INTERVAL_MS = 200

/**
 * 全局单一 daemon 生命周期管理：spawn / 解析启动日志 / 轮询 /health / kill。
 *
 * 不变量：整个 Electron 生命周期内仅 spawn 一次（符合 M9 q3「全局单一 daemon」）；
 * 项目根切换（M9.3）不会触发 daemon 重启。
 */
export class DaemonManager {
  private child: ChildProcess | null = null
  private config: DaemonConfig | null = null
  private crashed = false

  async start(): Promise<DaemonConfig> {
    const python = await locatePython()
    const child = spawn(python, ['-m', 'agent.cli', 'daemon'], {
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env },
    })
    this.child = child

    child.stdout?.on('data', (d: Buffer) => this.ingest(d.toString()))
    child.stderr?.on('data', (d: Buffer) => this.ingest(d.toString()))
    child.on('error', (err) => {
      console.error('[daemon] spawn error:', err)
    })
    child.on('exit', (code, signal) => {
      if (this.child !== child) return
      if (code !== null && code !== 0) {
        this.crashed = true
        console.error(`[daemon] 子进程异常退出 code=${code} signal=${signal}`)
      }
      this.child = null
    })

    const config = await this.waitForReady()
    this.config = config
    return config
  }

  /** 从 daemon 启动日志（stderr/stdout）解析 ws / health 地址。 */
  private ingest(text: string): void {
    const parsed = parseDaemonLog(text)
    if (parsed?.wsUrl && parsed?.healthUrl) {
      this.config = { wsUrl: parsed.wsUrl, healthUrl: parsed.healthUrl, token: '' }
      return
    }
    if (parsed?.wsUrl && !this.config) {
      this.config = {
        wsUrl: parsed.wsUrl,
        healthUrl: `http://${DEFAULT_HOST}:${DEFAULT_HEALTH_PORT}/health`,
        token: '',
      }
    }
  }

  private async waitForReady(): Promise<DaemonConfig> {
    const fallback: DaemonConfig = {
      wsUrl: '',
      healthUrl: `http://${DEFAULT_HOST}:${DEFAULT_HEALTH_PORT}/health`,
      token: '',
    }
    const deadline = Date.now() + START_TIMEOUT_MS
    while (Date.now() < deadline) {
      const cfg = this.config ?? fallback
      if (cfg.healthUrl && (await ping(cfg.healthUrl))) {
        return cfg
      }
      if (this.crashed) {
        throw new Error(
          'agentrunner daemon 进程异常退出，请检查 Python 环境与依赖（pip install -e ".[dev]"）。',
        )
      }
      await delay(HEALTH_POLL_INTERVAL_MS)
    }
    throw new Error('agentrunner daemon 在超时内未就绪（/health 未返回 200）。')
  }

  getConfig(): DaemonConfig | null {
    return this.config
  }

  isCrashed(): boolean {
    return this.crashed
  }

  /** 退出前终止 daemon 子进程（SIGTERM），避免孤儿进程。 */
  stop(): void {
    if (this.child) {
      this.child.kill('SIGTERM')
      this.child = null
    }
  }
}

/**
 * 解析 daemon 启动日志行，提取 ws / health 地址。
 * 支持两种格式：完整 `ws=... health=.../health`，或仅 `ws=...`（health 回退默认端口）。
 * 返回 null 表示未匹配任何信息。
 */
export function parseDaemonLog(
  text: string,
): { wsUrl: string; healthUrl: string } | null {
  const full = text.match(/ws=(ws:\/\/[^\s]+).*?health=(https?:\/\/[^\s]+)/)
  if (full) {
    return { wsUrl: full[1], healthUrl: full[2] }
  }
  const wsOnly = text.match(/ws=(ws:\/\/[^\s]+)/)
  if (wsOnly) {
    return { wsUrl: wsOnly[1], healthUrl: '' }
  }
  return null
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function ping(url: string): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
      res.resume()
      resolve(res.statusCode === 200)
    })
    req.on('error', () => resolve(false))
    req.setTimeout(1000, () => {
      req.destroy()
      resolve(false)
    })
  })
}
