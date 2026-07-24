import React from 'react'
import type { SessionInfo } from '../../protocol/types'

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
    <div style={{ flex: 1, overflowY: 'auto', padding: '4px 8px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '4px 4px' }}>
        <span style={{ fontSize: 12, color: '#666' }}>会话（{list.length}）</span>
        <button onClick={onCreate} style={{ fontSize: 12 }}>
          + 新建
        </button>
      </div>
      {list.length === 0 ? (
        <div style={{ padding: 12, color: '#999', fontSize: 13 }}>
          当前项目（<code>{projectRoot || '—'}</code>）无会话，
          <button onClick={onCreate} style={{ marginLeft: 6 }}>
            新建会话
          </button>
        </div>
      ) : (
        <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
          {list.map((s) => {
            const active = s.id === activeId
            return (
              <li
                key={s.id}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  padding: '6px 8px',
                  borderRadius: 6,
                  cursor: 'pointer',
                  background: active ? '#e8f0fe' : 'transparent',
                }}
                onClick={() => onOpen(s.id)}
              >
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {s.name ?? s.id.slice(0, 8)}
                  {s.persisted ? ' · 历史' : ''}
                </span>
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    onFork(s.id)
                  }}
                  title="fork 出新会话"
                  style={{ fontSize: 11 }}
                >
                  fork
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
