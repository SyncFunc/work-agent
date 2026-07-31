// 在前端计算 unified diff，替代对后端 diff 字符串的依赖。
// 使用 npm diff 库（createTwoFilesPatch），输出标准 unified-diff 格式，
// 与 backend _make_diff 兼容，可直接交给 DiffView 渲染。

import { createTwoFilesPatch } from 'diff'

/** unified diff 文本的增删行统计（标题旁 +x -x 用）。 */
export function countDiffStats(diffText: string): { added: number; removed: number } {
  let added = 0
  let removed = 0
  for (const ln of diffText.split('\n')) {
    if (ln.startsWith('+') && !ln.startsWith('+++')) added += 1
    else if (ln.startsWith('-') && !ln.startsWith('---')) removed += 1
  }
  return { added, removed }
}

/**
 * DiffBlock 的核心派生逻辑（纯函数，便于测试）：
 * 从 ToolBlock 状态推导「流式目标内容」与「实时 diff 文本」。
 *
 * 每个 delta 都更新 target（delta 本身由 LLM 分片产生，频率可接受），
 * 保证写入过程中实时展示 diff，而不是等完整行或结果。
 * 完整 content（tool_use 后）始终最新。
 */
export function computeThrottledTarget(opts: {
  fullContent: string | undefined
  streamingContent: string | null
  lastTarget: string
}): { target: string; nextLastTarget: string } {
  const { fullContent, streamingContent } = opts
  if (fullContent !== undefined) {
    return { target: fullContent, nextLastTarget: fullContent }
  }
  const partial = streamingContent ?? ''
  if (partial === '') return { target: '', nextLastTarget: '' }
  return { target: partial, nextLastTarget: partial }
}

/**
 * 从原始内容与新内容生成 standard unified diff。
 * @param original  写/改操作前的文件内容（空字符串 = 新文件）
 * @param modified  写/改操作后的文件内容
 * @param filePath  文件路径（仅用于 diff 头部 a/ b/ 标注）
 * @returns         与 backend _make_diff 兼容的 unified-diff 字符串
 */
export function computeUnifiedDiff(
  original: string,
  modified: string,
  filePath: string,
): string {
  return createTwoFilesPatch(`a/${filePath}`, `b/${filePath}`, original, modified, '', '')
}

/**
 * 从 tool_call_delta 累积的 JSON 片段中尽力提取 path 字段。
 * 与 extractPartialContent 同思路：先完整 parse，失败则正则逼近。
 */
export function extractPartialPath(deltaArgs: string): string | null {
  if (!deltaArgs) return null
  try {
    const parsed = JSON.parse(deltaArgs)
    if (typeof parsed.path === 'string' && parsed.path) return parsed.path
    return null
  } catch {
    const m = deltaArgs.match(/"path"\s*:\s*"((?:[^"\\]|\\.)*)"/)
    return m ? m[1] : null
  }
}

/**
 * 从 tool_call_delta 累积的 JSON 片段中尽力提取 content 字段。
 *
 * 流式参数 tc_args 是部分（可能不完整）的 JSON，例如：
 *   `{"path": "foo.py", "content": "import os\n\ndef h`
 * 本函数尝试：
 *   1. 完整 JSON.parse → 取 .content
 *   2. 失败则用正则逼近提取
 *
 * @returns 提取到的 content 文本，无法提取时返回 null。
 */
export function extractPartialContent(deltaArgs: string): string | null {
  if (!deltaArgs) return null

  // 尝试完整解析
  try {
    const parsed = JSON.parse(deltaArgs)
    if (typeof parsed.content === 'string') return parsed.content
    return null
  } catch {
    // 非完整 JSON：定位 "content": 之后的串值
    const idx = deltaArgs.indexOf('"content"')
    if (idx === -1) return null

    // 在 "content" 后面找第一个冒号
    const afterKey = deltaArgs.slice(idx + 9) // skip '"content"'
    const colon = afterKey.indexOf(':')
    if (colon === -1) return null

    const afterColon = afterKey.slice(colon + 1).trimStart()
    if (!afterColon.startsWith('"')) return null

    // 去掉开头的引号
    const raw = afterColon.slice(1)
    let result = ''
    let i = 0
    while (i < raw.length) {
      const ch = raw[i]
      if (ch === '"' && (i === 0 || raw[i - 1] !== '\\')) {
        // 未转义的闭合引号 → 内容结束（可能被截断后没有真正的闭合，取到末尾即可）
        break
      }
      if (ch === '\\' && i + 1 < raw.length) {
        const next = raw[i + 1]
        if (next === 'n') result += '\n'
        else if (next === 't') result += '\t'
        else if (next === '"') result += '"'
        else if (next === '\\') result += '\\'
        else result += ch + next
        i += 2
        continue
      }
      result += ch
      i++
    }
    return result || null
  }
}
