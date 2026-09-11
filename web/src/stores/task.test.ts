import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import type { TaskSnapshot } from '../types'
import { useTaskStore } from './task'

describe('task store', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    window.localStorage.clear()
    setActivePinia(createPinia())
  })

  afterEach(() => vi.unstubAllGlobals())

  it('starts idle without leaking state between tasks', () => {
    const store = useTaskStore()
    expect(store.task).toBeUndefined()
    expect(store.isRunning).toBe(false)
    expect(store.error).toBe('')
  })

  it('only clears the current analysis when a new analysis is explicitly started', () => {
    window.localStorage.setItem('analyse-agent.current-task-id', 'task-123')
    setActivePinia(createPinia())
    const store = useTaskStore()

    expect(store.currentTaskId).toBe('task-123')

    store.startNewAnalysis()

    expect(store.currentTaskId).toBe('')
    expect(window.localStorage.getItem('analyse-agent.current-task-id')).toBeNull()
  })

  it('ignores a task load that finishes after a new analysis starts', async () => {
    let finishLoad!: (task: TaskSnapshot) => void
    vi.spyOn(api, 'getTask').mockReturnValue(new Promise((resolve) => { finishLoad = resolve }))
    vi.spyOn(api, 'listRuns').mockResolvedValue([])
    const store = useTaskStore()

    const pending = store.loadTask('task-123')
    store.startNewAnalysis()
    finishLoad({ id: 'task-123', status: 'ready' } as TaskSnapshot)
    await pending

    expect(store.task).toBeUndefined()
    expect(store.currentTaskId).toBe('')
  })

  it('merges streamed artifacts without polling the task snapshot', async () => {
    class TestEventSource {
      static latest: TestEventSource
      listeners = new Map<string, EventListenerOrEventListenerObject[]>()
      onerror: ((event: Event) => unknown) | null = null

      constructor(readonly url: string) { TestEventSource.latest = this }
      addEventListener(name: string, listener: EventListenerOrEventListenerObject) {
        this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener])
      }
      close() {}
      emit(name: string, value: unknown) {
        const event = new MessageEvent(name, { data: JSON.stringify(value) })
        for (const listener of this.listeners.get(name) ?? []) {
          if (typeof listener === 'function') listener(event)
          else listener.handleEvent(event)
        }
      }
    }
    vi.stubGlobal('EventSource', TestEventSource)
    const getTask = vi.spyOn(api, 'getTask').mockResolvedValue({
      id: 'task-123', status: 'executing', progress: 50, status_message: '正在执行',
    } as TaskSnapshot)
    vi.spyOn(api, 'listRuns').mockResolvedValue([])
    vi.spyOn(api, 'listArtifacts').mockResolvedValue([])
    const store = useTaskStore()
    await store.loadTask('task-123')
    await store.loadArtifacts('task-123', 'run-1')

    TestEventSource.latest.emit('artifact.created', {
      schema_version: 1,
      id: 8,
      task_id: 'task-123',
      event_type: 'artifact.created',
      status: 'executing',
      progress: 60,
      message: '收入汇总',
      payload: {
        run_id: 'run-1',
        artifact: {
          id: 'artifact-1', task_id: 'task-123', run_id: 'run-1', sequence: 1,
          artifact_type: 'query', status: 'ready', title: '收入汇总', payload: { row_count: 1 },
          evidence_refs: [], created_at: '2026-09-11T00:00:00Z', updated_at: '2026-09-11T00:00:00Z',
        },
      },
      created_at: '2026-09-11T00:00:00Z',
    })

    expect(store.task?.progress).toBe(60)
    expect(store.task?.status_message).toBe('正在执行')
    expect(store.artifactsForRun('run-1')).toHaveLength(1)
    expect(store.artifactsForRun('run-1')[0].title).toBe('收入汇总')
    expect(getTask).toHaveBeenCalledTimes(1)
  })
})
