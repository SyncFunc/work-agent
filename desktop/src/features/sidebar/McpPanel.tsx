import { useEffect, useState } from 'react'
import { Plug, Plus, Trash2, X, Cable } from 'lucide-react'
import type { DaemonClient } from '../../protocol/client'
import type { McpServerInfo } from '../../protocol/types'
import { IconButton } from '../../components'
import './SpecPanels.css'

interface Props {
  client: DaemonClient | null
  projectRoot: string
  onClose: () => void
}

const SOURCE_LABEL: Record<string, string> = { builtin: '内建', user: '用户级', project: '项目级' }

interface FormState {
  name: string
  command: string
  args: string
  env: string
  scope: 'user' | 'project'
  enabled: boolean
}

const EMPTY_FORM: FormState = {
  name: '',
  command: '',
  args: '',
  env: '',
  scope: 'project',
  enabled: true,
}

export function McpPanel({ client, projectRoot, onClose }: Props): React.ReactElement {
  const [servers, setServers] = useState<McpServerInfo[] | null>(null)
  const [form, setForm] = useState<FormState>(EMPTY_FORM)
  const [adding, setAdding] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)

  const refresh = (): void => {
    if (client) client.command('mcp', null, projectRoot)
  }

  useEffect(() => {
    if (!client) return
    setServers(null)
    const off = client.onMessage('show_mcp', (env) => {
      const arr = (env.payload['servers'] as Array<Record<string, unknown>>) ?? []
      setServers(
        arr.map((s) => ({
          name: String(s.name ?? '?'),
          source: String(s.source ?? 'project'),
          command: String(s.command ?? ''),
          args: (s.args as string[] | undefined) ?? [],
          env: (s.env as Record<string, string> | undefined) ?? {},
          cwd: (s.cwd as string | null | undefined) ?? null,
          enabled: Boolean(s.enabled ?? true),
        })),
      )
    })
    client.command('mcp', null, projectRoot)
    return off
  }, [client, projectRoot])

  const flash = (kind: 'ok' | 'err', text: string): void => {
    setNotice({ kind, text })
    setTimeout(() => setNotice(null), 3000)
  }

  const isBuiltin = (s: McpServerInfo): boolean => s.source === 'builtin'

  const toggle = (s: McpServerInfo): void => {
    if (!client || isBuiltin(s)) return
    client.updateMcp('toggle', s.name, { enabled: !s.enabled, scope: s.source === 'user' ? 'user' : 'project', projectRoot })
    flash('ok', `${s.name} ${!s.enabled ? '已启用' : '已禁用'}`)
    setTimeout(refresh, 300)
  }

  const remove = (s: McpServerInfo): void => {
    if (!client || isBuiltin(s)) return
    if (!window.confirm(`删除 MCP Server「${s.name}」？`)) return
    client.updateMcp('remove', s.name, { scope: s.source === 'user' ? 'user' : 'project', projectRoot })
    flash('ok', `已删除 ${s.name}`)
    setTimeout(refresh, 300)
  }

  const submit = (): void => {
    if (!client) return
    if (!form.name.trim() || !form.command.trim()) {
      flash('err', 'Server 名称和命令必填')
      return
    }
    const args = form.args
      .split(/\s+/)
      .map((a) => a.trim())
      .filter(Boolean)
    const env: Record<string, string> = {}
    for (const line of form.env.split('\n')) {
      const i = line.indexOf('=')
      if (i > 0) env[line.slice(0, i).trim()] = line.slice(i + 1).trim()
    }
    client.updateMcp('add', form.name.trim(), {
      command: form.command.trim(),
      args,
      env,
      enabled: form.enabled,
      scope: form.scope,
      projectRoot,
    })
    flash('ok', `已${editing ? '更新' : '接入'} ${form.name.trim()}`)
    setAdding(false)
    setEditing(null)
    setForm(EMPTY_FORM)
    setTimeout(refresh, 300)
  }

  const startEdit = (s: McpServerInfo): void => {
    setEditing(s.name)
    setAdding(true)
    setForm({
      name: s.name,
      command: s.command ?? '',
      args: (s.args ?? []).join(' '),
      env: Object.entries(s.env ?? {})
        .map(([k, v]) => `${k}=${v}`)
        .join('\n'),
      scope: s.source === 'user' ? 'user' : 'project',
      enabled: Boolean(s.enabled ?? true),
    })
  }

  return (
    <section className="wa-specview">
      <div className="wa-specview__head">
        <span className="wa-specview__icon">
          <Plug size={18} />
        </span>
        <div className="wa-specview__titles">
          <h2 className="wa-specview__title">MCP 服务</h2>
          <span className="wa-specview__hint">接入外部工具（Model Context Protocol）。可启用/禁用、新增、编辑、删除。</span>
        </div>
        <IconButton icon={<X size={16} />} label="返回聊天" onClick={onClose} />
      </div>

      <div className="wa-specview__body">
        {notice && (
          <div className={`wa-mcp__notice wa-mcp__notice--${notice.kind}`}>{notice.text}</div>
        )}

        <div className="wa-specview__toolbar">
          <span className="wa-specview__hint">来源与启停</span>
          {!adding && (
            <button className="wa-mcp__add-btn" onClick={() => setAdding(true)}>
              <Plus size={14} /> 接入新服务
            </button>
          )}
        </div>

        {adding && (
          <div className="wa-mcp__form">
            <div className="wa-mcp__form-row">
              <input
                className="wa-mcp__input"
                placeholder="Server 名称（唯一）"
                value={form.name}
                disabled={!!editing}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
              />
              <input
                className="wa-mcp__input wa-mcp__input--wide"
                placeholder="command，如 npx"
                value={form.command}
                onChange={(e) => setForm({ ...form, command: e.target.value })}
              />
            </div>
            <div className="wa-mcp__form-row">
              <input
                className="wa-mcp__input wa-mcp__input--wide"
                placeholder="args（空格分隔），如 -y github-mcp-server"
                value={form.args}
                onChange={(e) => setForm({ ...form, args: e.target.value })}
              />
            </div>
            <div className="wa-mcp__form-row">
              <textarea
                className="wa-mcp__textarea"
                placeholder="env（每行 KEY=VALUE），如：TOKEN=${GITHUB_TOKEN}"
                value={form.env}
                onChange={(e) => setForm({ ...form, env: e.target.value })}
              />
            </div>
            <div className="wa-mcp__form-row">
              <select
                className="wa-mcp__select"
                value={form.scope}
                onChange={(e) => setForm({ ...form, scope: e.target.value as 'user' | 'project' })}
              >
                <option value="project">项目级（.agent/mcp.yaml）</option>
                <option value="user">用户级（~/.agent/mcp.yaml）</option>
              </select>
              <label className="wa-mcp__toggle">
                <input
                  type="checkbox"
                  checked={form.enabled}
                  onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
                />
                <span className="wa-mcp__track">
                  <span className="wa-mcp__thumb" />
                </span>
                <span>{form.enabled ? '启用' : '禁用'}</span>
              </label>
            </div>
            <div className="wa-mcp__form-actions">
              <button className="wa-mcp__btn-primary" onClick={submit}>
                {editing ? '保存' : '接入'}
              </button>
              <button
                className="wa-mcp__btn-ghost"
                onClick={() => {
                  setAdding(false)
                  setEditing(null)
                  setForm(EMPTY_FORM)
                }}
              >
                取消
              </button>
            </div>
          </div>
        )}

        {servers === null ? (
          <div className="wa-specview__loading">加载中…</div>
        ) : servers.length === 0 ? (
          <div className="wa-specview__empty">暂无 MCP 服务，点击「接入新服务」添加。</div>
        ) : (
          <ul className="wa-specview__list">
            {servers.map((s) => (
              <li key={s.name} className="wa-mcp__card">
                <div className="wa-mcp__card-head">
                  <Cable size={15} className="wa-mcp__card-icon" />
                  <span className="wa-mcp__card-name">{s.name}</span>
                  <span className={`wa-mcp__badge wa-mcp__badge--${s.source}`}>
                    {SOURCE_LABEL[s.source] ?? s.source}
                  </span>
                  <label className={`wa-mcp__toggle wa-mcp__toggle--sm${isBuiltin(s) ? ' wa-mcp__toggle--disabled' : ''}`}>
                    <input type="checkbox" checked={!!s.enabled} disabled={isBuiltin(s)} onChange={() => toggle(s)} />
                    <span className="wa-mcp__track">
                      <span className="wa-mcp__thumb" />
                    </span>
                  </label>
                </div>
                <div className="wa-mcp__card-cmd">
                  <code>{s.command ?? ''}</code>
                  {(s.args ?? []).length > 0 && <code className="wa-mcp__args">{s.args!.join(' ')}</code>}
                </div>
                <div className="wa-mcp__card-actions">
                  {isBuiltin(s) ? (
                    <span className="wa-mcp__builtin-tag">内建 · 随代码分发</span>
                  ) : (
                    <>
                      <button className="wa-mcp__btn-ghost" onClick={() => startEdit(s)}>
                        编辑
                      </button>
                      <button className="wa-mcp__btn-danger" onClick={() => remove(s)}>
                        <Trash2 size={13} /> 删除
                      </button>
                    </>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}
