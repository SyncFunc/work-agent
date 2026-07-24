// 真机冒烟：启动真实 daemon，验证 M9.0 多项目隔离 + M9.3 列表过滤。
// 在 A 项目建 2 个会话、B 项目建 1 个；listSessions(A) 应只返回 2 个，listSessions(B) 只返回 1 个。
// 仅本地验证用（非 CI 必跑）。用法：node desktop/scripts/smoke-sessions.mjs

import { spawn } from 'node:child_process'
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const TIMEOUT_MS = 20000

function locatePython() {
  for (const bin of [process.env.AGENT_PYTHON, 'python', 'python3'].filter(Boolean)) {
    try {
      if (spawn(bin, ['--version']).pid) return bin
    } catch {
      /* next */
    }
  }
  throw new Error('未找到 python')
}

function parseDaemonLog(text) {
  const full = text.match(/ws=(ws:\/\/[^\s]+).*?health=(https?:\/\/[^\s]+)/)
  if (full) return { wsUrl: full[1] }
  const wsOnly = text.match(/ws=(ws:\/\/[^\s]+)/)
  return wsOnly ? { wsUrl: wsOnly[1] } : null
}

const projA = mkdtempSync(join(tmpdir(), 'm9a-'))
const projB = mkdtempSync(join(tmpdir(), 'm9b-'))

// 为每个临时项目写入最小 settings（仅占位 api_key；会话不真正调用模型，无需真实密钥）。
// daemon 按 project_root 加载 settings 并构建 Model，缺失 api_key 会在 session.new 时报错。
function seedSettings(dir) {
  mkdirSync(join(dir, '.agent'), { recursive: true })
  writeFileSync(join(dir, '.agent', 'settings.yaml'), 'llm:\n  api_key: sk-smoke-test\n')
}
seedSettings(projA)
seedSettings(projB)

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

child.stdout.on('data', (d) => {
  const t = d.toString()
  const p = parseDaemonLog(t)
  if (p?.wsUrl) wsUrl = p.wsUrl
  if (process.env.SMOKE_DEBUG) process.stderr.write('[daemon stdout] ' + t)
})
child.stderr.on('data', (d) => {
  const t = d.toString()
  const p = parseDaemonLog(t)
  if (p?.wsUrl) wsUrl = p.wsUrl
  if (process.env.SMOKE_DEBUG) process.stderr.write('[daemon stderr] ' + t)
})

const timer = setTimeout(() => {
  console.error('FAIL: 超时')
  cleanup()
  process.exit(1)
}, TIMEOUT_MS)

function waitForWs() {
  return new Promise((res, rej) => {
    const t0 = Date.now()
    const iv = setInterval(() => {
      if (wsUrl) {
        clearInterval(iv)
        res(wsUrl)
      } else if (Date.now() - t0 > TIMEOUT_MS) {
        clearInterval(iv)
        rej(new Error('未解析 ws'))
      }
    }, 100)
  })
}

function send(ws, type, payload) {
  ws.send(JSON.stringify({ type, payload }))
}

async function main() {
  if (typeof WebSocket === 'undefined') throw new Error('需 Node 22+ 全局 WebSocket')
  const url = await waitForWs()
  if (process.env.SMOKE_DEBUG) process.stderr.write('[smoke] wsUrl=' + url + '\n')
  const ws = new WebSocket(url)
  const created = []
  const lists = {}
  let pending = 3 // 期望创建的会话数
  let listed = 0

  const done = new Promise((resolve, reject) => {
    ws.onopen = () => {
      send(ws, 'hello', { client_type: 'smoke', version: '0.1.0' })
      // 连接建立后再发会话创建请求（避免 Sent before connected）
      send(ws, 'session.new', { project_root: projA, name: 'A1' })
      send(ws, 'session.new', { project_root: projA, name: 'A2' })
      send(ws, 'session.new', { project_root: projB, name: 'B1' })
    }
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.type === 'session.created') {
        created.push(msg.payload.session_id)
        if (--pending === 0) {
          // 两个项目的列表查询
          send(ws, 'session.list', { project_root: projA })
          send(ws, 'session.list', { project_root: projB })
        }
      } else if (msg.type === 'session_list') {
        lists[msg.payload.project_root] = msg.payload.sessions
        if (++listed === 2) resolve()
      } else if (msg.type === 'error') {
        reject(new Error('daemon error: ' + JSON.stringify(msg.payload)))
      }
    }
    ws.onerror = (e) => reject(new Error('ws error ' + String(e?.message ?? e)))
  })

  // 创建会话请求已在 onopen 中发出
  await done

  const aCount = (lists[projA] ?? []).length
  const bCount = (lists[projB] ?? []).length
  if (aCount !== 2 || bCount !== 1) {
    console.error(`FAIL: 隔离不符 A=${aCount} (期望2) B=${bCount} (期望1)`)
    cleanup()
    process.exit(1)
  }
  console.log(`OK: 多项目隔离正确 A=${aCount} B=${bCount}`)
  clearTimeout(timer)
  ws.close()
  cleanup()
  process.exit(0)
}

main().catch((e) => {
  console.error('FAIL:', String(e))
  clearTimeout(timer)
  cleanup()
  process.exit(1)
})
