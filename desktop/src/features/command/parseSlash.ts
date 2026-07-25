// 斜杠命令解析（纯函数，可单测）：输入以 / 开头 → {name, args}。

export function parseSlash(text: string): { name: string; args: string } | null {
  const t = text.trim()
  if (!t.startsWith('/')) return null
  const body = t.slice(1)
  if (body.length === 0) return null
  const sp = body.indexOf(' ')
  if (sp < 0) return { name: body, args: '' }
  return { name: body.slice(0, sp), args: body.slice(sp + 1).trim() }
}
