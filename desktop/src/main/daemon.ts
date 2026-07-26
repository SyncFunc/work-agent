import { app } from 'electron'
import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync } from 'node:fs'
import { join } from 'node:path'
import http from 'node:http'
import { locatePython } from './python'
import type { DaemonConfig, DaemonStage } from '../shared/daemon-config'

const DEFAULT_HOST = '127.0.0.1'
const DEFAULT_HEALTH_PORT = 18790
const START_TIMEOUT_MS = 15000
const HEALTH_POLL_INTERVAL_MS = 200

/** 打包态下随安装包分发的冻结 daemon 二进制文件名（按平台区分扩展名）。 */
function bundledDaemonName(): string {
  return process.platform === 'win32' ? 'daemon.exe' : 'daemon'
}

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

  async start(onStage?: (stage: DaemonStage) => void): Promise<DaemonConfig> {
    const { command, args } = await this.resolveDaemonCommand()
    const child = spawn(command, args, {
      stdio: ['ignore', 'pipe', 'pipe'],
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

    onStage?.('spawning')
    onStage?.('waiting')
    const config = await this.waitForReady()
    onStage?.('ready')
    this.config = config
    return config
  }

  /**
   * 解析 daemon 启动命令：三档优先级，决定「用什么拉起子进程」。
   *
   * 1. `AGENT_DAEMON_BIN` 环境变量指向的冻结二进制（本地联调冻结产物用）；
   * 2. 打包态（`app.isPackaged`）：随安装包分发的冻结二进制
   *    `resources/daemon/daemon[.exe]`，**脱离主机 Python 环境**（方案 A）；
   * 3. 本地开发态（未打包）：走原有 `python -m agent.cli daemon` 路径。
   */
  private async resolveDaemonCommand(): Promise<{ command: string; args: string[] }> {
    const override = process.env.AGENT_DAEMON_BIN
    if (override && existsSync(override)) {
      return { command: override, args: [] }
    }
    if (app.isPackaged) {
      const bin = join(process.resourcesPath, 'daemon', bundledDaemonName())
      if (existsSync(bin)) {
        return { command: bin, args: [] }
      }
      console.warn('[daemon] 打包态未找到冻结二进制，回退系统 Python:', bin)
    }
    const python = await locatePython()
    return { command: python, args: ['-m', 'agent.cli', 'daemon'] }
  }

  /** 从 daemon 启动日志（stderr/stdout）解析 ws / health 地址。 */
  private ingest(text: string): void {
    const parsed = parseDaemonLog(text)
    if (parsed?.wsUrl && parsed?.healthUrl) {
      this.config = {
        wsUrl: parsed.wsUrl,
        healthUrl: parsed.healthUrl,
        token: '',
      }
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
          'agentrunner daemon 进程异常退出。请检查 daemon 二进制（打包态）或 Python 环境与依赖（pip install -e ".[dev]"）。',
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
