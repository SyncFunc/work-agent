// M9.7 可观测面板：daemon trace 查询的薄封装（请求/响应配对在 DaemonClient 内完成）。

import { DaemonClient } from '../../protocol/client'
import type { TraceListResponse, TraceTreeResponse } from '../../protocol/types'

export function listTraces(
  client: DaemonClient,
  projectRoot: string,
  sessionId?: string,
): Promise<TraceListResponse> {
  return client.listTraces(projectRoot, sessionId)
}

export function getTrace(
  client: DaemonClient,
  projectRoot: string,
  traceId: string,
): Promise<TraceTreeResponse> {
  return client.getTrace(projectRoot, traceId)
}
