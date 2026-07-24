import React, { useEffect, useState } from 'react'
import type { DaemonConfig } from '../shared/daemon-config'

export default function App(): React.ReactElement {
  const [config, setConfig] = useState<DaemonConfig | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    window.agentApi
      .getDaemonConfig()
      .then(setConfig)
      .catch((e: unknown) => setError(String(e)))
  }, [])

  return (
    <div style={{ fontFamily: 'system-ui, sans-serif', padding: 24 }}>
      <h1>Work Agent</h1>
      {error && <p style={{ color: 'crimson' }}>配置加载失败：{error}</p>}
      {config ? (
        <div>
          <p>daemon 已就绪：</p>
          <ul>
            <li>
              WebSocket: <code>{config.wsUrl || '（尚未解析，等待 /health）'}</code>
            </li>
            <li>Token: {config.token ? '已设置' : '未设置（本机回环，无需鉴权）'}</li>
            <li>
              Health: <code>{config.healthUrl}</code>
            </li>
          </ul>
          <p style={{ color: '#888' }}>
            （渲染层 WebSocket 连接管理将在 M9.2 实现）
          </p>
        </div>
      ) : (
        <p>正在连接 daemon…</p>
      )}
    </div>
  )
}
