import { describe, it, expect } from 'vitest'
import { initialState, sessionsReducer, type SessionsState } from './sessionMachine'
import type { AgentEvent } from '../../protocol/types'

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

  it('replay 期间不追加（累积在前端 buffer），replayEnd 整体替换 events', () => {
    let s = sessionsReducer(initialState('/p'), {
      type: 'sessionCreated',
      id: 'x',
      name: 'X',
      projectRoot: '/p',
    })
    s = sessionsReducer(s, { type: 'replayStart' })
    expect(s.replaying).toBe(true)
    // 期间收到事件【不应】追加到 tab.events（累积改由前端 ReplayBuffer 负责），
    // 否则会与 replayEnd 的整体替换叠加，导致历史重复 / 越来越多。
    expect(s.tabs[0].events).toEqual([])
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

  it('sessionRenamed 更新列表项 title 与 name', () => {
    let s: SessionsState = {
      ...initialState('/p'),
      list: [
        { id: '1', name: 'A', title: '首个提问', project_root: '/p' },
        { id: '2', name: 'B', project_root: '/p' },
      ],
    }
    s = sessionsReducer(s, { type: 'sessionRenamed', id: '1', title: '手动标题' })
    expect(s.list.find((x) => x.id === '1')).toMatchObject({
      title: '手动标题',
      name: '手动标题',
    })
    expect(s.list.find((x) => x.id === '2')?.title).toBeUndefined()
  })

  it('sessionRenamed 对未知 id 不改变列表', () => {
    let s: SessionsState = {
      ...initialState('/p'),
      list: [{ id: '1', name: 'A', project_root: '/p' }],
    }
    const before = s.list
    s = sessionsReducer(s, { type: 'sessionRenamed', id: 'nope', title: 'x' })
    expect(s.list).toEqual(before)
  })

  it('liveEvent 忽略归属其他会话的事件（切换后旧会话输出不串渲染）', () => {
    let s: SessionsState = {
      ...initialState('/p'),
      tabs: [{ id: '1', name: 'A', projectRoot: '/p', events: [] }],
      activeId: '1',
    }
    const evBase = { seq: 1, type: 'text' as const, ts: 0 }
    // 事件归属会话 2（非 active）→ 忽略
    s = sessionsReducer(s, { type: 'liveEvent', event: { ...evBase, text: '旧会话', session_id: '2' } })
    expect(s.tabs[0].events).toHaveLength(0)
    // 事件归属 active 会话 1 → 接收
    s = sessionsReducer(s, { type: 'liveEvent', event: { ...evBase, text: '当前', session_id: '1' } })
    expect(s.tabs[0].events).toHaveLength(1)
    // 无 session_id（兼容回放/旧数据）→ 接收
    s = sessionsReducer(s, { type: 'liveEvent', event: { ...evBase, text: '兼容' } })
    expect(s.tabs[0].events).toHaveLength(2)
  })
})
