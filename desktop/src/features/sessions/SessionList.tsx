import React from 'react'
import type { SessionInfo } from '../../protocol/types'
import { IconButton } from '../../components'
import { Plus, GitBranch } from 'lucide-react'

interface Props {
  list: SessionInfo[]
  activeId: string | null
  projectRoot: string
  onOpen: (id: string) => void
  onCreate: () => void
  onFork: (id: string) => void
}

/** 当前项目下的会话列表（session.list -> session_list）。空状态展示「新建会话」入口。 */
export function SessionList({ list, activeId, projectRoot, onOpen, onCreate, onFork }: Props): React.ReactElement {
  return (
    <div className="wa-session">
      <div className="wa-session-head">
        <span className="wa-session-head__label">会话（{list.length}）</span>
        <IconButton icon={<Plus size={16} />} label="新建会话" onClick={onCreate} />
      </div>
      {list.length === 0 ? (
        <div className="wa-session-empty">
          当前项目（<code>{projectRoot || '—'}</code>）无会话，点击上方「+ 新建」创建。
        </div>
      ) : (
        <ul className="wa-session-list">
          {list.map((s) => {
            const active = s.id === activeId
            return (
              <li
                key={s.id}
                className={`wa-session-item${active ? ' wa-session-item--active' : ''}`}
                onClick={() => onOpen(s.id)}
              >
                <span className="wa-session-item__name">
                  {s.name ?? s.id.slice(0, 8)}
                  {s.persisted ? ' · 历史' : ''}
                </span>
                <IconButton
                  icon={<GitBranch size={14} />}
                  label="fork 出新会话"
                  size="sm"
                  onClick={(e) => {
                    e.stopPropagation()
                    onFork(s.id)
                  }}
                />
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
