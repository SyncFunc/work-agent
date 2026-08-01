// 多会话状态机的纯 reducer（无 React/DOM 依赖，可单测）。
// 状态由 DaemonClient 的消息驱动；replay 期间的历史事件整体替换激活 tab 的 events，
// 实时事件（非 replay）追加到激活 tab——与 daemon 的 replay_start/end 一致。

import type { AgentEvent, SessionInfo } from '../../protocol/types'

export interface SessionTab {
  id: string
  name: string
  projectRoot: string
  events: AgentEvent[]
}

export interface SessionsState {
  projectRoot: string
  tabs: SessionTab[]
  activeId: string | null
  list: SessionInfo[]
  replaying: boolean
}

export type SessionsAction =
  | { type: 'setProjectRoot'; projectRoot: string }
  | { type: 'sessionList'; list: SessionInfo[] }
  | { type: 'sessionCreated'; id: string; name: string | null; projectRoot: string }
  | { type: 'attached'; id: string; projectRoot: string }
  | { type: 'replayStart' }
  | { type: 'replayEnd'; events: AgentEvent[] }
  | { type: 'liveEvent'; event: AgentEvent }
  | { type: 'closeTab'; id: string }
  | { type: 'sessionDeleted'; id: string }
  | { type: 'sessionRenamed'; id: string; title: string }

export function initialState(projectRoot: string): SessionsState {
  return { projectRoot, tabs: [], activeId: null, list: [], replaying: false }
}

function tabIndex(tabs: SessionTab[], id: string): number {
  return tabs.findIndex((t) => t.id === id)
}

function upsertTab(
  tabs: SessionTab[],
  id: string,
  projectRoot: string,
  name: string | null,
): SessionTab[] {
  const i = tabIndex(tabs, id)
  const existing = i >= 0 ? tabs[i] : null
  // 已有 tab 时保留其名称（attach 消息不带 name）；新建或 session.created 带 name 时用之。
  const resolvedName = name && name.length > 0 ? name : existing ? existing.name : id.slice(0, 8)
  const tab: SessionTab = {
    id,
    name: resolvedName,
    projectRoot,
    events: existing ? existing.events : [],
  }
  if (i >= 0) {
    const next = tabs.slice()
    next[i] = tab
    return next
  }
  return [...tabs, tab]
}

/** 按 id 更新或追加一条会话列表项（新建会话后即时刷新左侧列表，无需等 daemon 重发 session_list）。 */
function upsertSessionInfo(list: SessionInfo[], info: SessionInfo): SessionInfo[] {
  const i = list.findIndex((s) => s.id === info.id)
  if (i >= 0) {
    const next = list.slice()
    next[i] = { ...next[i], ...info }
    return next
  }
  return [...list, info]
}

export function sessionsReducer(state: SessionsState, action: SessionsAction): SessionsState {
  switch (action.type) {
    case 'setProjectRoot':
      // 切换项目根：清掉当前项目的 UI 会话/列表（标签页也可能属其它项目，按 projectRoot 过滤保留）。
      return {
        ...state,
        projectRoot: action.projectRoot,
        list: [],
        tabs: state.tabs.filter((t) => t.projectRoot === action.projectRoot),
        activeId: null,
        replaying: false,
      }

    case 'sessionList':
      return { ...state, list: action.list }

    case 'sessionCreated':
      return {
        ...state,
        // 新建后即时把会话加入左侧列表（daemon 不会自动重发 session_list）。
        list: upsertSessionInfo(state.list, {
          id: action.id,
          name: action.name ?? null,
          project_root: action.projectRoot,
          persisted: false,
          attached: true,
        }),
        tabs: upsertTab(state.tabs, action.id, action.projectRoot, action.name),
        activeId: action.id,
      }

    case 'attached': {
      const tabs = upsertTab(state.tabs, action.id, action.projectRoot, null)
      return { ...state, tabs, activeId: action.id, replaying: false }
    }

    case 'replayStart':
      return { ...state, replaying: true }

    case 'replayEnd': {
      if (state.activeId === null) return { ...state, replaying: false }
      const i = tabIndex(state.tabs, state.activeId)
      if (i < 0) return { ...state, replaying: false }
      const tab = state.tabs[i]
      const next = state.tabs.slice()
      // 历史整体替换（去掉 replay 期间累积的占位），保持瞬时事件排除规则由 daemon 保证。
      next[i] = { ...tab, events: action.events }
      return { ...state, tabs: next, replaying: false }
    }

    case 'liveEvent': {
      if (state.replaying || state.activeId === null) return state
      // M11.6：切换会话后，旧会话仍在输出的实时事件会带其 session_id；若事件归属
      // 非当前 active 会话，则忽略（避免旧会话内容串渲染到当前会话）。无 session_id
      // 的兼容数据（回放/旧版本）仍按 active 会话处理。
      const evSid = action.event.session_id
      if (typeof evSid === 'string' && evSid !== state.activeId) return state
      const i = tabIndex(state.tabs, state.activeId)
      if (i < 0) return state
      const tab = state.tabs[i]
      const next = state.tabs.slice()
      next[i] = { ...tab, events: [...tab.events, action.event] }
      return { ...state, tabs: next }
    }

    case 'closeTab': {
      const tabs = state.tabs.filter((t) => t.id !== action.id)
      const activeId = state.activeId === action.id ? (tabs[0]?.id ?? null) : state.activeId
      return { ...state, tabs, activeId }
    }

    // M9.9 步骤6：会话彻底删除后，从列表/标签/激活态中移除。
    case 'sessionDeleted': {
      const list = state.list.filter((s) => s.id !== action.id)
      const tabs = state.tabs.filter((t) => t.id !== action.id)
      const activeId = state.activeId === action.id ? (tabs[0]?.id ?? null) : state.activeId
      return { ...state, list, tabs, activeId }
    }

    // M11.6：用户手动设置标题后，即时更新左侧列表项的 title（持久化由 daemon 负责）。
    case 'sessionRenamed': {
      const i = state.list.findIndex((s) => s.id === action.id)
      if (i < 0) return state
      const list = state.list.slice()
      list[i] = { ...list[i], title: action.title, name: action.title }
      return { ...state, list }
    }

    default:
      return state
  }
}
