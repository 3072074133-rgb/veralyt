import { defineStore } from 'pinia'
import { computed, ref, shallowRef } from 'vue'
import { api } from '../api'
import type { EvidenceRecord, RunArtifact, TaskEvent, TaskSnapshot, UploadBatchResponse, UploadFailure, WorkflowRun } from '../types'

const terminal = new Set(['ready', 'off_topic', 'needs_clarification', 'needs_review', 'completed_with_warnings', 'completed', 'failed', 'cancelled'])
const CURRENT_TASK_KEY = 'analyse-agent.current-task-id'

function savedTaskId() {
  return typeof window === 'undefined' ? '' : window.localStorage.getItem(CURRENT_TASK_KEY) ?? ''
}

export const useTaskStore = defineStore('task', () => {
  const task = ref<TaskSnapshot>()
  const busy = ref(false)
  const error = ref('')
  const evidence = ref<EvidenceRecord>()
  const runs = ref<WorkflowRun[]>([])
  const artifactsByRun = shallowRef<Record<string, RunArtifact[]>>({})
  const uploadFailures = ref<UploadFailure[]>([])
  const currentTaskId = ref(savedTaskId())
  let source: EventSource | undefined
  let refreshPending = false
  let loadGeneration = 0
  const artifactLoadGenerations = new Map<string, number>()

  const isRunning = computed(() => !!task.value && !terminal.has(task.value.status))

  function rememberTask(id: string) {
    currentTaskId.value = id
    window.localStorage.setItem(CURRENT_TASK_KEY, id)
  }

  async function createTask() {
    closeEvents()
    runs.value = []
    artifactsByRun.value = {}
    uploadFailures.value = []
    const id = await api.createTask()
    task.value = await api.getTask(id)
    rememberTask(id)
    return id
  }

  async function loadTask(id: string) {
    const generation = ++loadGeneration
    closeEvents()
    uploadFailures.value = []
    const [loaded, loadedRuns] = await Promise.all([api.getTask(id), api.listRuns(id)])
    if (generation !== loadGeneration) return
    if (task.value?.id !== loaded.id) artifactsByRun.value = {}
    task.value = loaded
    runs.value = loadedRuns
    rememberTask(id)
    if (!terminal.has(task.value.status)) connectEvents(id)
  }

  function startNewAnalysis() {
    loadGeneration += 1
    closeEvents()
    task.value = undefined
    evidence.value = undefined
    runs.value = []
    artifactsByRun.value = {}
    uploadFailures.value = []
    error.value = ''
    currentTaskId.value = ''
    window.localStorage.removeItem(CURRENT_TASK_KEY)
  }

  async function upload(files: File[]): Promise<UploadBatchResponse | undefined> {
    if (!task.value) return undefined
    busy.value = true
    error.value = ''
    uploadFailures.value = []
    try {
      const result = await api.uploadFiles(task.value.id, files)
      task.value = result.task
      uploadFailures.value = result.rejected_files
      if (result.outcome === 'failed') error.value = result.rejected_files.map((item) => `${item.name}：${item.message}`).join('；')
      return result
    }
    catch (reason) { error.value = reason instanceof Error ? reason.message : '上传失败' }
    finally { busy.value = false }
  }

  async function send(content: string) {
    if (!content.trim() || busy.value || isRunning.value) return
    busy.value = true
    error.value = ''
    try {
      const id = task.value?.id ?? await createTask()
      task.value = await api.sendMessage(id, content)
      runs.value = await api.listRuns(task.value.id)
      connectEvents(task.value.id)
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : '发送失败'
      throw reason
    } finally { busy.value = false }
  }

  async function openEvidence(id: string) {
    if (task.value) evidence.value = await api.getEvidence(task.value.id, id)
  }

  function artifactsForRun(runId: string) {
    return artifactsByRun.value[runId] ?? []
  }

  async function loadArtifacts(taskId: string, runId: string) {
    const generation = (artifactLoadGenerations.get(runId) ?? 0) + 1
    artifactLoadGenerations.set(runId, generation)
    try {
      const loaded = await api.listArtifacts(taskId, runId)
      if (artifactLoadGenerations.get(runId) !== generation || task.value?.id !== taskId) return
      const merged = new Map(loaded.map((item) => [item.id, item]))
      for (const item of artifactsForRun(runId)) merged.set(item.id, item)
      artifactsByRun.value = {
        ...artifactsByRun.value,
        [runId]: [...merged.values()].sort((left, right) => left.sequence - right.sequence),
      }
    } catch (reason) {
      if (!artifactsByRun.value[runId]?.length) {
        error.value = reason instanceof Error ? reason.message : '分析附件加载失败'
      }
    }
  }

  function mergeArtifact(artifact: RunArtifact) {
    if (task.value?.id !== artifact.task_id) return
    const merged = new Map(artifactsForRun(artifact.run_id).map((item) => [item.id, item]))
    merged.set(artifact.id, artifact)
    artifactsByRun.value = {
      ...artifactsByRun.value,
      [artifact.run_id]: [...merged.values()].sort((left, right) => left.sequence - right.sequence),
    }
  }

  async function cancelQueuedRun() {
    if (!task.value?.pending_run_id || task.value.queue_position === 0) return
    error.value = ''
    try {
      await api.cancelRun(task.value.id, task.value.pending_run_id)
      const [updated, updatedRuns] = await Promise.all([api.getTask(task.value.id), api.listRuns(task.value.id)])
      task.value = updated
      runs.value = updatedRuns
      closeEvents()
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : '取消失败'
      throw reason
    }
  }
  async function stopCurrentRun() {
    if (!task.value?.pending_run_id) return
    error.value = ''
    try {
      await api.cancelRun(task.value.id, task.value.pending_run_id)
      await loadTask(task.value.id)
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : '中止失败'
      throw reason
    }
  }

  function connectEvents(id: string) {
    closeEvents()
    const connection = new EventSource(`/api/v1/tasks/${id}/events`)
    source = connection
    const names = [
      'task.created',
      'task.updated',
      'conversation.compacting',
      'report.progress',
      'conversation.compacted',
      'conversation.compaction_failed',
      'run.activated',
      'run.failed',
      'run.replay_failed',
      'run.cancelled',
      'artifact.created',
    ]
    names.forEach((name) => connection.addEventListener(name, handleEvent as EventListener))
    const snapshotEvents = new Set([
      'run.activated',
    ])

    function handleEvent(raw: MessageEvent<string>) {
      let event: TaskEvent
      try { event = JSON.parse(raw.data) as TaskEvent }
      catch { return }
      if (event.task_id !== id) return
      if (event.schema_version !== 1) { void refresh(); return }
      if (task.value?.id === id) {
        task.value = {
          ...task.value,
          status: event.status,
          progress: event.progress,
          status_message: event.event_type === 'artifact.created' ? task.value.status_message : event.message,
        }
      }
      if (event.event_type === 'artifact.created') {
        const artifact = event.payload.artifact
        if (isRunArtifact(artifact)) mergeArtifact(artifact)
      }
      if (snapshotEvents.has(event.event_type) || terminal.has(event.status)) void refresh()
    }

    async function refresh() {
      if (refreshPending) return
      refreshPending = true
      try {
        const [updated, updatedRuns] = await Promise.all([api.getTask(id), api.listRuns(id)])
        if (source === connection && updated.id === id) {
          task.value = updated
          runs.value = updatedRuns
        }
        if (terminal.has(updated.status)) {
          const runId = updated.pending_run_id ?? updated.active_run_id
          if (runId) await loadArtifacts(id, runId)
          if (source === connection) closeEvents()
        }
      } catch (reason) {
        error.value = reason instanceof Error ? reason.message : '任务状态刷新失败'
      } finally {
        refreshPending = false
      }
    }
    connection.onerror = () => { if (task.value && terminal.has(task.value.status)) closeEvents() }
  }

  function closeEvents() { source?.close(); source = undefined }
  return { task, runs, busy, error, evidence, artifactsByRun, uploadFailures, currentTaskId, isRunning, createTask, loadTask, startNewAnalysis, upload, send, openEvidence, artifactsForRun, loadArtifacts, cancelQueuedRun, stopCurrentRun, closeEvents }
})

function isRunArtifact(value: unknown): value is RunArtifact {
  if (!value || typeof value !== 'object') return false
  const artifact = value as Partial<RunArtifact>
  return typeof artifact.id === 'string'
    && typeof artifact.task_id === 'string'
    && typeof artifact.run_id === 'string'
    && typeof artifact.sequence === 'number'
    && typeof artifact.artifact_type === 'string'
    && typeof artifact.status === 'string'
    && typeof artifact.title === 'string'
    && !!artifact.payload
    && Array.isArray(artifact.evidence_refs)
}
