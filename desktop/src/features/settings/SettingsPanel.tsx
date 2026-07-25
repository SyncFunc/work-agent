// 设置面板：编辑 LLM/计划/澄清/UI 主题/沙箱/审批配置，写回项目级 settings.yaml。
// 仅持久化，不做热重载（新建会话/重启 daemon 后生效），保存后提示。

import React, { useEffect, useState } from 'react'
import type { SettingsShape } from './settingsApi'
import { applyTheme, loadSettings, saveSettings, type Theme } from './settingsApi'

interface Props {
  projectRoot: string
  onClose: () => void
}

export function SettingsPanel({ projectRoot, onClose }: Props): React.ReactElement {
  const [s, setS] = useState<SettingsShape>({})
  const [saved, setSaved] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let alive = true
    void loadSettings(projectRoot).then((v) => {
      if (alive) setS(v)
    })
    return () => {
      alive = false
    }
  }, [projectRoot])

  const patch = (fn: (draft: SettingsShape) => void): void => {
    const next: SettingsShape = JSON.parse(JSON.stringify(s ?? {}))
    fn(next)
    setS(next)
    setSaved(false)
  }

  const save = async (): Promise<void> => {
    setBusy(true)
    await saveSettings(projectRoot, s)
    if (s.ui?.theme) applyTheme((s.ui.theme as Theme) ?? 'light')
    setBusy(false)
    setSaved(true)
  }

  const llm = s.llm ?? {}
  const plan = s.plan ?? {}
  const clarify = s.clarify ?? {}
  const ui = s.ui ?? {}
  const sandbox = s.sandbox ?? {}
  const approval = s.approval ?? {}

  return (
    <div className="wa-modal" onClick={onClose}>
      <div className="wa-modal-box" style={{ maxWidth: 560 }} onClick={(e) => e.stopPropagation()}>
        <h3 style={{ marginTop: 0 }}>设置 · {projectRoot || '(未选择项目)'}</h3>

        <fieldset style={{ border: '1px solid #eee', borderRadius: 8, padding: 10 }}>
          <legend>LLM</legend>
          <label style={labelStyle}>model
            <input style={inputStyle} value={llm.model ?? ''} onChange={(e) => patch((d) => { d.llm = { ...llm, model: e.target.value } })} />
          </label>
          <label style={labelStyle}>base_url
            <input style={inputStyle} value={llm.base_url ?? ''} onChange={(e) => patch((d) => { d.llm = { ...llm, base_url: e.target.value } })} />
          </label>
          <label style={labelStyle}>api_key
            <input style={inputStyle} type="password" value={llm.api_key ?? ''} placeholder="留空不修改" onChange={(e) => patch((d) => { d.llm = { ...llm, api_key: e.target.value } })} />
          </label>
        </fieldset>

        <fieldset style={{ border: '1px solid #eee', borderRadius: 8, padding: 10, marginTop: 8 }}>
          <legend>计划 / 澄清 / 沙箱 / 审批 / 主题</legend>
          <label style={labelStyle}>plan.mode
            <input style={inputStyle} value={plan.mode ?? ''} onChange={(e) => patch((d) => { d.plan = { ...plan, mode: e.target.value } })} />
          </label>
          <label style={labelStyle}>clarify.enabled
            <input type="checkbox" checked={clarify.enabled === true} onChange={(e) => patch((d) => { d.clarify = { ...clarify, enabled: e.target.checked } })} />
          </label>
          <label style={labelStyle}>sandbox.profile
            <input style={inputStyle} value={sandbox.profile ?? ''} onChange={(e) => patch((d) => { d.sandbox = { ...sandbox, profile: e.target.value } })} />
          </label>
          <label style={labelStyle}>approval.mode
            <input style={inputStyle} value={approval.mode ?? ''} onChange={(e) => patch((d) => { d.approval = { ...approval, mode: e.target.value } })} />
          </label>
          <label style={labelStyle}>ui.theme
            <select value={ui.theme ?? 'light'} onChange={(e) => patch((d) => { d.ui = { ...ui, theme: e.target.value } })}>
              <option value="light">light</option>
              <option value="dark">dark</option>
            </select>
          </label>
        </fieldset>

        <div style={{ marginTop: 12, display: 'flex', gap: 8, justifyContent: 'flex-end', alignItems: 'center' }}>
          {saved && <span style={{ color: '#2e7d32', fontSize: 12 }}>已保存（新会话/重启 daemon 生效）</span>}
          <button type="button" onClick={onClose}>关闭</button>
          <button type="button" disabled={busy} onClick={() => void save()}>保存</button>
        </div>
      </div>
    </div>
  )
}

const labelStyle: React.CSSProperties = { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center', margin: '4px 0' }
const inputStyle: React.CSSProperties = { flex: 1, marginLeft: 8, maxWidth: 320 }
