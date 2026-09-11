<script setup lang="ts">
import { computed, watch } from 'vue'
import { BarChart3, CheckCircle2, ChevronDown, Code2, ExternalLink, Table2, TriangleAlert } from 'lucide-vue-next'
import { useTaskStore } from '../stores/task'
import type { RunArtifact } from '../types'

const props = defineProps<{ taskId: string; runId: string; status: string }>()
const emit = defineEmits<{ openEvidence: [id: string] }>()
const store = useTaskStore()
const artifacts = computed(() => store.artifactsForRun(props.runId))
const icons = { query: Code2, table: Table2, chart: BarChart3, validation: CheckCircle2, error: TriangleAlert }
const typeLabels = { query: '查询', table: '数据表', chart: '图表', validation: '校验', error: '错误' }
const statusLabels = { pending: '等待中', running: '处理中', ready: '就绪', failed: '失败' }

watch(() => [props.taskId, props.runId], () => void store.loadArtifacts(props.taskId, props.runId), { immediate: true })

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}
function rows(item: RunArtifact) {
  return Array.isArray(item.payload.rows)
    ? item.payload.rows.filter((row): row is Record<string, unknown> => !!row && typeof row === 'object' && !Array.isArray(row)).slice(0, 8)
    : []
}
function columns(item: RunArtifact) {
  const explicit = Array.isArray(item.payload.columns) ? item.payload.columns.filter((value): value is string => typeof value === 'string') : []
  return explicit.length ? explicit : Object.keys(rows(item)[0] ?? {})
}
function rowCount(item: RunArtifact) {
  return typeof item.payload.row_count === 'number' ? item.payload.row_count : rows(item).length
}
function summary(item: RunArtifact) {
  return typeof item.payload.summary === 'string' ? item.payload.summary : ''
}
function sql(item: RunArtifact) {
  const arguments_ = asRecord(item.payload.arguments)
  return typeof arguments_.sql === 'string' ? arguments_.sql : ''
}
function warnings(item: RunArtifact) {
  return Array.isArray(item.payload.warnings) ? item.payload.warnings.filter((value): value is string => typeof value === 'string') : []
}
function validationIssues(item: RunArtifact) {
  if (!Array.isArray(item.payload.issues)) return []
  return item.payload.issues.map((value) => asRecord(value)).filter((value) => typeof value.message === 'string')
}
function errorMessage(item: RunArtifact) {
  return typeof item.payload.error === 'string' ? item.payload.error : '执行失败，未返回详细错误。'
}
function chartMeta(item: RunArtifact) {
  return asRecord(item.payload.chart)
}
function formatCell(value: unknown) {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
</script>

<template>
  <section v-if="artifacts.length" class="result-panel artifact-panel" aria-live="polite">
    <header><strong>分析过程</strong><span>{{ artifacts.length }} 项 · 实时更新</span></header>
    <div class="artifact-list">
      <details v-for="item in artifacts" :key="item.id" class="artifact-item" :open="item.status === 'failed'">
        <summary>
          <span class="artifact-icon"><component :is="icons[item.artifact_type]" :size="16" /></span>
          <span class="artifact-title"><strong>{{ item.title }}</strong><small>{{ typeLabels[item.artifact_type] }}<template v-if="['query', 'table'].includes(item.artifact_type)"> · {{ rowCount(item) }} 行</template></small></span>
          <span :class="['artifact-status', item.status]">{{ statusLabels[item.status] }}</span>
          <ChevronDown class="artifact-chevron" :size="15" />
        </summary>

        <div class="artifact-body">
          <p v-if="summary(item)" class="artifact-summary">{{ summary(item) }}</p>

          <template v-if="item.artifact_type === 'query' || item.artifact_type === 'table'">
            <div v-if="sql(item)" class="artifact-sql"><span>SQL</span><pre><code>{{ sql(item) }}</code></pre></div>
            <div v-if="rows(item).length" class="artifact-table-wrap">
              <table>
                <thead><tr><th v-for="column in columns(item)" :key="column">{{ column }}</th></tr></thead>
                <tbody><tr v-for="(row, index) in rows(item)" :key="index"><td v-for="column in columns(item)" :key="column">{{ formatCell(row[column]) }}</td></tr></tbody>
              </table>
              <small v-if="rowCount(item) > rows(item).length">仅展示前 {{ rows(item).length }} 行，共 {{ rowCount(item) }} 行</small>
            </div>
          </template>

          <div v-else-if="item.artifact_type === 'validation'" class="artifact-validation">
            <strong>{{ item.payload.passed ? '数据与证据校验通过' : '发现需要复核的问题' }}</strong>
            <ul v-if="validationIssues(item).length"><li v-for="(issue, index) in validationIssues(item)" :key="index" :class="String(issue.severity ?? '')">{{ issue.message }}</li></ul>
          </div>

          <div v-else-if="item.artifact_type === 'chart'" class="artifact-chart-meta">
            <BarChart3 :size="18" />
            <span>{{ chartMeta(item).chart_type ? `${chartMeta(item).chart_type} · ` : '' }}图表已加入分析结果</span>
          </div>

          <p v-else class="artifact-error">{{ errorMessage(item) }}</p>

          <ul v-if="warnings(item).length" class="artifact-warnings"><li v-for="warning in warnings(item)" :key="warning">{{ warning }}</li></ul>
          <div v-if="item.evidence_refs.length" class="artifact-evidence">
            <button v-for="(evidenceId, index) in item.evidence_refs" :key="evidenceId" class="button" @click="emit('openEvidence', evidenceId)"><ExternalLink :size="13" />证据 {{ index + 1 }}</button>
          </div>
        </div>
      </details>
    </div>
  </section>
</template>
