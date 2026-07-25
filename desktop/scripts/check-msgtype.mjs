// 契约检查：比对 Python 端 agent/daemon/protocol.py 的 MsgType 枚举值
// 与 TS 端 desktop/src/protocol/types.ts 的 ALL_MSG_TYPES 集合。
// 二者不一致则退出码 1（防 Python/TS 协议漂移）。
// 由 tests/unit/test_m9_protocol_contract.py 通过 `node` 调用。

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = join(here, '..', '..') // desktop/scripts -> repo root
const protocolPy = join(repoRoot, 'agent', 'daemon', 'protocol.py')
const typesTs = join(repoRoot, 'desktop', 'src', 'protocol', 'types.ts')

function pyMsgTypes(src) {
  const block = src.split('class MsgType')[1]?.split('@runtime_checkable')[0]
  if (!block) throw new Error('未能在 protocol.py 定位 MsgType 枚举块')
  const values = []
  const re = /^\s*([A-Z_][A-Z0-9_]*)\s*=\s*"([^"]+)"/gm
  let m
  while ((m = re.exec(block)) !== null) values.push(m[2])
  return values
}

function tsMsgTypes(src) {
  const m = src.match(/ALL_MSG_TYPES\s*=\s*\[([\s\S]*?)\]/)
  if (!m) throw new Error('未能在 types.ts 定位 ALL_MSG_TYPES')
  const values = []
  const re = /["']([^"']+)["']/g
  let x
  while ((x = re.exec(m[1])) !== null) values.push(x[1])
  return values
}

function fail(msg) {
  console.error('FAIL: ' + msg)
  process.exit(1)
}

let py, ts
try {
  py = pyMsgTypes(readFileSync(protocolPy, 'utf8'))
  ts = tsMsgTypes(readFileSync(typesTs, 'utf8'))
} catch (e) {
  fail(String(e))
}

const pySet = new Set(py)
const tsSet = new Set(ts)

if (pySet.size !== tsSet.size) {
  fail(`MsgType 数量不一致：Python=${py.length} TS=${ts.length}`)
}

const onlyPy = [...pySet].filter((v) => !tsSet.has(v))
const onlyTs = [...tsSet].filter((v) => !pySet.has(v))
if (onlyPy.length || onlyTs.length) {
  fail(
    `MsgType 集合不一致：\n  仅 Python 有: ${onlyPy.join(', ') || '(无)'}\n  仅 TS 有: ${onlyTs.join(', ') || '(无)'}`,
  )
}

console.log(`OK: Python 与 TS 的 MsgType 一致（${pySet.size} 项）`)
