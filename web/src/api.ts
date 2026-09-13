import type { DatasetAsset, DatasetAssetDetail, DatasetCorrectionRequest, DatasetCorrectionResult, DatasetPreview, DatasetProfile, DatasetRelationship, EvidenceRecord, KnowledgeBaseDetail, KnowledgeBaseSummary, KnowledgeDocumentInput, KnowledgeMatch, ReportDetail, ReportJob, ReportSummary, RunArtifact, TaskKnowledgeBinding, TaskListResponse, TaskSnapshot, UploadBatchResponse, WorkflowRun } from './types'

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options)
  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    try { message = (await response.json()).detail ?? message } catch { /* response was not JSON */ }
    throw new Error(message)
  }
  return response.status === 204 ? (undefined as T) : response.json()
}

export const api = {
  async createTask(): Promise<string> {
    const result = await request<{id: string}>('/api/v1/tasks', { method: 'POST' })
    return result.id
  },
  getTask: (id: string) => request<TaskSnapshot>(`/api/v1/tasks/${id}`),
  getTaskRelationships: (taskId: string) => request<DatasetRelationship[]>(`/api/v1/tasks/${taskId}/relationships`),
  saveTaskRelationships: (taskId: string, relationships: DatasetRelationship[]) => request<DatasetRelationship[]>(`/api/v1/tasks/${taskId}/relationships`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ relationships }) }),
  getDatasetPreview: (taskId: string, datasetId: string, page = 1, pageSize = 50) => request<DatasetPreview>(`/api/v1/tasks/${taskId}/datasets/${datasetId}/preview?page=${page}&page_size=${pageSize}`),
  getDatasetProfile: (taskId: string, datasetId: string) => request<DatasetProfile>(`/api/v1/tasks/${taskId}/datasets/${datasetId}/profile`),
  correctDataset: (taskId: string, datasetId: string, payload: DatasetCorrectionRequest) => request<DatasetCorrectionResult>(`/api/v1/tasks/${taskId}/datasets/${datasetId}/corrections`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) }),
  listDatasets: (includeArchived = false) => request<{items: DatasetAsset[]; total: number}>(`/api/v1/datasets?include_archived=${includeArchived}`),
  getDataset: (id: string) => request<DatasetAssetDetail>(`/api/v1/datasets/${id}`),
  archiveDataset: (id: string) => request<void>(`/api/v1/datasets/${id}`, { method: 'DELETE' }),
  createTaskFromDataset: (datasetId: string, revisionId: string) => request<{id: string; status: string}>(`/api/v1/datasets/${datasetId}/revisions/${revisionId}/tasks`, { method: 'POST' }),
  listKnowledgeBases: (includeArchived = false) => request<{items: KnowledgeBaseSummary[]; total: number}>(`/api/v1/knowledge-bases?include_archived=${includeArchived}`),
  getKnowledgeBase: (id: string) => request<KnowledgeBaseDetail>(`/api/v1/knowledge-bases/${id}`),
  createKnowledgeBase: (payload: {name: string; description: string; documents: KnowledgeDocumentInput[]; change_summary: string}) => request<KnowledgeBaseDetail>('/api/v1/knowledge-bases', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) }),
  publishKnowledgeRevision: (id: string, payload: {documents: KnowledgeDocumentInput[]; change_summary: string}) => request<KnowledgeBaseDetail>(`/api/v1/knowledge-bases/${id}/revisions`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) }),
  archiveKnowledgeBase: (id: string) => request<void>(`/api/v1/knowledge-bases/${id}`, { method: 'DELETE' }),
  getTaskKnowledgeBases: (taskId: string) => request<TaskKnowledgeBinding[]>(`/api/v1/tasks/${taskId}/knowledge-bases`),
  setTaskKnowledgeBases: (taskId: string, bindings: Array<{knowledge_base_id: string; revision_id: string}>) => request<TaskKnowledgeBinding[]>(`/api/v1/tasks/${taskId}/knowledge-bases`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({bindings}) }),
  getRunKnowledge: (taskId: string, runId: string) => request<KnowledgeMatch[]>(`/api/v1/tasks/${taskId}/runs/${runId}/knowledge`),
  async uploadFiles(id: string, files: File[]) {
    const form = new FormData()
    files.forEach((file) => form.append('files', file))
    const response = await fetch(`/api/v1/tasks/${id}/files`, { method: 'POST', body: form })
    const payload = await response.json() as UploadBatchResponse | {detail?: string}
    if ('outcome' in payload) return payload
    throw new Error(payload.detail ?? `上传失败（${response.status}）`)
  },
  async uploadDatasetFiles(files: File[]) {
    const task = await request<{id: string}>('/api/v1/tasks', { method: 'POST' })
    try {
      const form = new FormData()
      files.forEach((file) => form.append('files', file))
      const response = await fetch(
        `/api/v1/tasks/${task.id}/files?publish_to_library=true`,
        { method: 'POST', body: form },
      )
      const payload = await response.json() as UploadBatchResponse | {detail?: string}
      if ('outcome' in payload) return payload
      throw new Error(payload.detail ?? `上传失败（${response.status}）`)
    } finally {
      await request<void>(`/api/v1/tasks/${task.id}`, { method: 'DELETE' }).catch(() => undefined)
    }
  },
  sendMessage: (id: string, content: string) => request<TaskSnapshot>(`/api/v1/tasks/${id}/messages`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ content }) }),
  async listTasks(query = '', status = '', page = 1, pageSize = 20) {
    const params = new URLSearchParams({ query, page: String(page), page_size: String(pageSize) })
    if (status) params.set('status', status)
    return request<TaskListResponse>(`/api/v1/tasks?${params}`)
  },
  deleteTask: (id: string) => request<void>(`/api/v1/tasks/${id}`, { method: 'DELETE' }),
  getEvidence: (taskId: string, evidenceId: string) => request<EvidenceRecord>(`/api/v1/tasks/${taskId}/evidence/${evidenceId}`),
  listRuns: (taskId: string) => request<WorkflowRun[]>(`/api/v1/tasks/${taskId}/runs`),
  listArtifacts: (taskId: string, runId: string) => request<RunArtifact[]>(`/api/v1/tasks/${taskId}/runs/${runId}/artifacts`),
  activateRun: (taskId: string, runId: string) => request<WorkflowRun>(`/api/v1/tasks/${taskId}/runs/${runId}/activate`, { method: 'POST' }),
  cancelRun: (taskId: string, runId: string) => request<void>(`/api/v1/tasks/${taskId}/runs/${runId}`, { method: 'DELETE' }),
  publishReport: (taskId: string) => request<ReportJob>(`/api/v1/tasks/${taskId}/reports`, { method: 'POST' }),
  getReportJob: (id: string) => request<ReportJob>(`/api/v1/report-jobs/${id}`),
  listReports: () => request<ReportSummary[]>('/api/v1/reports'),
  getReport: (id: string) => request<ReportDetail>(`/api/v1/reports/${id}`),
}
