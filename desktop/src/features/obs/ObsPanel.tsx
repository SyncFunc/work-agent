// ObsPanel：M9.7 可观测面板主容器——状态栏 + 可切换的 Trace / 日志 / 后台 视图。

import { useState } from 'react'
import type { ReactNode } from 'react'
import { DaemonClient } from '../../protocol/client'
import { BackgroundAgents } from './BackgroundAgents'
import { LogView } from './LogView'
import { StatusBar } from './StatusBar'
import { TraceTree } from './TraceTree'
import { useObs } from './useObs'
import { IconButton, Tabs } from '../../components'
import { Bot, ListTree, ScrollText, X } from 'lucide-react'

interface Props {
  client: DaemonClient | null
  projectRoot: string
  sessionId: string | null
  contextWindow?: number
  onClose: () => void
  /** 窄窗下转抽屉（绝对定位覆盖）。 */
  drawer?: boolean
  /** 内联模式宽度。 */
  width?: number
  /** 拖拽改宽起点。 */
  onResizeStart?: (e: React.MouseEvent) => void
}

type Tab = 'trace' | 'log' | 'bg'

const TABS: { id: Tab; label: ReactNode }[] = [
  { id: 'trace', label: (<><ListTree size={14} /> Trace</>) },
  { id: 'log', label: (<><ScrollText size={14} /> 日志</>) },
  { id: 'bg', label: (<><Bot size={14} /> 后台</>) },
]

export function ObsPanel({ client, projectRoot, sessionId, contextWindow, onClose, drawer, width, onResizeStart }: Props) {
  const [tab, setTab] = useState<Tab>('trace')
  const obs = useObs(client)

  return (
    <>
      {!drawer && <div className="wa-resizer" onMouseDown={onResizeStart} />}
      <aside className={`wa-obs ${drawer ? 'wa-obs--drawer' : ''}`} style={drawer ? undefined : { width }}>
        <StatusBar
          projectRoot={projectRoot}
          sessionId={sessionId}
          usage={obs.usage}
          estimated={obs.estimated}
          mode={obs.mode}
          contextWindow={contextWindow}
        />
        <div className="wa-obs__tabs">
          <Tabs tabs={TABS} active={tab} onChange={(id) => setTab(id as Tab)} />
          <IconButton icon={<X size={16} />} label="关闭可观测面板" onClick={onClose} />
        </div>
        <div className="wa-obs__body">
          {tab === 'trace' && <TraceTree client={client} projectRoot={projectRoot} sessionId={sessionId} />}
          {tab === 'log' && <LogView logs={obs.logs} onClear={obs.clearLogs} />}
          {tab === 'bg' && <BackgroundAgents client={client} />}
        </div>
      </aside>
    </>
  )
}
