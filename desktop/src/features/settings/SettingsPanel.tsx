// 设置面板：编辑 LLM/计划/澄清/UI 主题/沙箱/审批配置，写回项目级 settings.yaml。
// 仅持久化，不做热重载（新建会话/重启 daemon 后生效），保存后弹出成功 Toast。

import React, { useEffect, useState } from 'react'
import type { SettingsShape } from './settingsApi'
import { applyTheme, loadSettings, saveSettings, type Theme } from './settingsApi'
import { Button, Modal } from '../../components'
import type { ToastKind } from '../../components'

interface Props {
  projectRoot: string
  onClose: () => void
  /** 保存成功后由宿主弹出 Toast（避免与通知堆叠重叠）。 */
  onToast: (kind: ToastKind, text: string) => void
}

export function SettingsPanel({ projectRoot, onClose, onToast }: Props): React.ReactElement {
  const [s, setS] = useState<SettingsShape>({})
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
  }

  const save = async (): Promise<void> => {
    setBusy(true)
    await saveSettings(projectRoot, s)
    if (s.ui?.theme) applyTheme((s.ui.theme as Theme) ?? 'light')
    setBusy(false)
    onToast('success', '已保存（新会话/重启 daemon 生效）')
  }

  const llm = s.llm ?? {}
  const plan = s.plan ?? {}
  const clarify = s.clarify ?? {}
  const ui = s.ui ?? {}
  const sandbox = s.sandbox ?? {}
  const approval = s.approval ?? {}

  return (
    <Modal
      open
      onClose={onClose}
      title={`设置 · ${projectRoot || '(未选择项目)'}`}
      footer={
        <>
          <Button onClick={onClose}>关闭</Button>
          <Button variant="primary" onClick={() => void save()} disabled={busy}>
            {busy ? '保存中…' : '保存'}
          </Button>
        </>
      }
    >
      <fieldset className="wa-fieldset">
        <legend>LLM</legend>
        <label className="wa-field">
          model
          <input
            className="wa-input"
            value={llm.model ?? ''}
            onChange={(e) => patch((d) => { d.llm = { ...llm, model: e.target.value } })}
          />
        </label>
        <label className="wa-field">
          base_url
          <input
            className="wa-input"
            value={llm.base_url ?? ''}
            onChange={(e) => patch((d) => { d.llm = { ...llm, base_url: e.target.value } })}
          />
        </label>
        <label className="wa-field">
          api_key
          <input
            className="wa-input"
            type="password"
            value={llm.api_key ?? ''}
            placeholder="留空不修改"
            onChange={(e) => patch((d) => { d.llm = { ...llm, api_key: e.target.value } })}
          />
        </label>
      </fieldset>

      <fieldset className="wa-fieldset">
        <legend>计划 / 澄清 / 沙箱 / 审批 / 主题</legend>
        <label className="wa-field">
          plan.mode
          <input
            className="wa-input"
            value={plan.mode ?? ''}
            onChange={(e) => patch((d) => { d.plan = { ...plan, mode: e.target.value } })}
          />
        </label>
        <label className="wa-field">
          <span>clarify.enabled</span>
          <input
            type="checkbox"
            checked={clarify.enabled === true}
            onChange={(e) => patch((d) => { d.clarify = { ...clarify, enabled: e.target.checked } })}
          />
        </label>
        <label className="wa-field">
          sandbox.profile
          <input
            className="wa-input"
            value={sandbox.profile ?? ''}
            onChange={(e) => patch((d) => { d.sandbox = { ...sandbox, profile: e.target.value } })}
          />
        </label>
        <label className="wa-field">
          approval.mode
          <input
            className="wa-input"
            value={approval.mode ?? ''}
            onChange={(e) => patch((d) => { d.approval = { ...approval, mode: e.target.value } })}
          />
        </label>
        <label className="wa-field">
          ui.theme
          <select
            className="wa-input"
            value={ui.theme ?? 'light'}
            onChange={(e) => patch((d) => { d.ui = { ...ui, theme: e.target.value } })}
          >
            <option value="light">light</option>
            <option value="dark">dark</option>
          </select>
        </label>
      </fieldset>
    </Modal>
  )
}
