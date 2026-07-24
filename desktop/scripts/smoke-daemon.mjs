// 真机冒烟：启动真实 daemon，用 Node 内置 WebSocket（DaemonClient 默认实现）完成
// hello -> welcome 握手，验证协议两端一致。仅用于本地验证（非 CI 必跑）。
// 用法：node desktop/scripts/smoke-daemon.mjs

import { spawn } from 'node:child_process'

const TIMEOUT_MS = 20000

function locatePython() {
  const order = [process.env.AGENT_PYTHON, 'python', 'python3'].filter(Boolean)
  for (const bin of order) {
    try {
      const p = spawn(bin, ['--version'])
      if (p.pid) return bin
    } catch {
      /* try next */
    }
  }
  throw new Error('未找到 python 解释器')
}

function parseDaemonLog(text) {
  const full = text.match(/ws=(ws:\/\/[^\s]+).*?health=(https?:\/\/[^\s]+)/)
  if (full) return { wsUrl: full[1], healthUrl: full[2] }
  const wsOnly = text.match(/ws=(ws:\/\/[^\s]+)/)
  if (wsOnly) return { wsUrl: wsOnly[1], healthUrl: '' }
  return null
}

const python = locatePython()
const child = spawn(python, ['-m', 'agent.cli', 'daemon'], {
  stdio: ['ignore', 'pipe', 'pipe'],
  env: { ...process.env },
})

let wsUrl = null
const cleanup = () => {
  try {
    child.kill('SIGTERM')
  } catch {
    /* ignore */
  }
}

const timer = setTimeout(() => {
  console.error('FAIL: 超时未收到 welcome')
  cleanup()
  process.exit(1)
}, TIMEOUT_MS)

child.stdout.on('data', (d) => {
  const parsed = parseDaemonLog(d.toString())
  if (parsed?.wsUrl) wsUrl = parsed.wsUrl
})
child.stderr.on('data', (d) => {
  const parsed = parseDaemonLog(d.toString())
  if (parsed?.wsUrl) wsUrl = parsed.wsUrl
})

function waitForWs() {
  return new Promise((resolve, reject) => {
    const started = Date.now()
    const poll = setInterval(() => {
      if (wsUrl) {
        clearInterval(poll)
        resolve(wsUrl)
      } else if (Date.now() - started > TIMEOUT_MS) {
        clearInterval(poll)
        reject(new Error('未从 daemon 日志解析到 ws 地址'))
      }
    }, 100)
  })
}

async function main() {
  if (typeof WebSocket === 'undefined') {
    throw new Error('当前 Node 无全局 WebSocket（需 Node 22+）')
  }
  const url = await waitForWs()
  const ws = new WebSocket(url)
  ws.onopen = () => {
    ws.send(JSON.stringify({ type: 'hello', payload: { client_type: 'smoke', version: '0.1.0' } }))
  }
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data)
    if (msg.type === 'welcome') {
      console.log('OK: 收到 welcome =', JSON.stringify(msg.payload))
      clearTimeout(timer)
      ws.close()
      cleanup()
      process.exit(0)
    }
  }
  ws.onerror = (e) => {
    console.error('FAIL: WebSocket 错误', String(e?.message ?? e))
    clearTimeout(timer)
    cleanup()
    process.exit(1)
  }
}

main().catch((e) => {
  console.error('FAIL:', String(e))
  clearTimeout(timer)
  cleanup()
  process.exit(1)
})
