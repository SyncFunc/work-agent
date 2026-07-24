// 校验 daemon 启动日志解析正则（与 src/main/daemon.ts 的 parseDaemonLog 保持一致）。
const re = /ws=(ws:\/\/[^\s]+).*?health=(https?:\/\/[^\s]+)/
const log =
  '[daemon] 已启动（多项目感知）：ws=ws://127.0.0.1:18789 health=http://127.0.0.1:18790/health'
const m = log.match(re)
if (!m) {
  console.error('FAIL: 未匹配真实日志行')
  process.exit(1)
}
console.log('wsUrl =', m[1])
console.log('healthUrl =', m[2])
if (m[1] !== 'ws://127.0.0.1:18789' || m[2] !== 'http://127.0.0.1:18790/health') {
  console.error('FAIL: 解析结果不符预期')
  process.exit(1)
}
console.log('OK: parseDaemonLog 正则匹配真实日志行通过')
