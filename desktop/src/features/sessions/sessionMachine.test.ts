import { describe, it, expect } from 'vitest'
import {
  initialState,
  sessionsReducer,
  type AgentEvent,
  type SessionsState,
} from './sessionMachine'

function ev(seq: number, type: AgentEvent['type'] = 'text'): AgentEvent {
  return { seq, type, ts: seq }
}

describe('sessionsReducer', () => {
  it('initialState', () => {
    const s = initialState('/p')
    expect(s.projectRoot).toBe('/p')
    expect(s.tabs).toEqual([])
    expect(s.activeId).toBeNull()
  })

  it('setProjectRoot 过滤其它项目 tab 并清空列表/激活', () => {
    let s: SessionsState = {
      ...initialState('/a'),
      tabs: [
        { id: '1', name: 'A', projectRoot: '/a', events: [] },
        { id: '2', name: 'B', projectRoot: '/b', events: [] },
      ],
      activeId: '1',
      list: [{ id: '1', name: 'A', project_root: '/a' }],
    }
    s = sessionsReducer(s, { type: 'setProjectRoot', projectRoot: '/b' })
    expect(s.projectRoot).toBe('/b')
    expect(s.tabs.map((t) => t.id)).toEqual(['2'])
    expect(s.activeId).toBeNull()
    expect(s.list).toEqual([])
  })

  it('sessionCreated 增加 tab 并激活', () => {
    const s = sessionsReducer(initialState('/p'), {
      type: 'sessionCreated',
      id: 'x',
      name: null,
      projectRoot: '/p',
    })
    expect(s.tabs).toHaveLength(1)
    expect(s.tabs[0].name).toBe('x'.slice(0, 8))
    expect(s.activeId).toBe('x')
  })

  it('attached 激活已存在 tab', () => {
    let s = sessionsReducer(initialState('/p'), {
      type: 'sessionCreated',
      id: 'x',
      name: '会话X',
      projectRoot: '/p',
    })
    s = sessionsReducer(s, { type: 'attached', id: 'x', projectRoot: '/p' })
    expect(s.activeId).toBe('x')
    expect(s.tabs[0].name).toBe('会话X')
  })

  it('replay 期间累积，replayEnd 整体替换 events', () => {
    let s = sessionsReducer(initialState('/p'), {
      type: 'sessionCreated',
      id: 'x',
      name: 'X',
      projectRoot: '/p',
    })
    s = sessionsReducer(s, { type: 'replayStart' })
    expect(s.replaying).toBe(true)
    // 期间收到几条事件（实时不应追加）
    s = sessionsReducer(s, { type: 'replayEvent', event: ev(0) })
    s = sessionsReducer(s, { type: 'replayEvent', event: ev(1) })
    expect(s.tabs[0].events).toHaveLength(2)
    // replay 结束：用历史整体替换
    const history = [ev(0), ev(1), ev(2)]
    s = sessionsReducer(s, { type: 'replayEnd', events: history })
    expect(s.replaying).toBe(false)
    expect(s.tabs[0].events).toEqual(history)
  })

  it('liveEvent 在 replay 外追加', () => {
    let s = sessionsReducer(initialState('/p'), {
      type: 'sessionCreated',
      id: 'x',
      name: 'X',
      projectRoot: '/p',
    })
    s = sessionsReducer(s, { type: 'liveEvent', event: ev(5) })
    expect(s.tabs[0].events).toEqual([ev(5)])
  })

  it('liveEvent 在 replay 期间不追加（防瞬时事件重复）', () => {
    let s = sessionsReducer(initialState('/p'), {
      type: 'sessionCreated',
      id: 'x',
      name: 'X',
      projectRoot: '/p',
    })
    s = sessionsReducer(s, { type: 'replayStart' })
    s = sessionsReducer(s, { type: 'liveEvent', event: ev(99) })
    expect(s.tabs[0].events).toEqual([])
  })

  it('closeTab 移除 tab 并切换激活', () => {
    let s: SessionsState = {
      ...initialState('/p'),
      tabs: [
        { id: '1', name: 'A', projectRoot: '/p', events: [] },
        { id: '2', name: 'B', projectRoot: '/p', events: [] },
      ],
      activeId: '1',
    }
    s = sessionsReducer(s, { type: 'closeTab', id: '1' })
    expect(s.tabs.map((t) => t.id)).toEqual(['2'])
    expect(s.activeId).toBe('2')
  })
})
