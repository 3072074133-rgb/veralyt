<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ChevronDown, ChevronRight, Download, FileText, RefreshCw, Trash2 } from 'lucide-vue-next'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message-box/style/css'
import { api } from '../api'
import type { ReportDetail, ReportSummary } from '../types'

const items = ref<ReportSummary[]>([])
const details = ref<Record<string, ReportDetail>>({})
const expanded = ref('')
const loading = ref(false)
onMounted(load)
async function load() {
  loading.value = true
  try {
    items.value = await api.listReports()
    const loaded = await Promise.all(items.value.map(async (item) => [item.id, await api.getReport(item.id)] as const))
    details.value = Object.fromEntries(loaded)
  }
  catch (reason) { ElMessage.error(reason instanceof Error ? reason.message : '报告加载失败') }
  finally { loading.value = false }
}
async function toggle(item: ReportSummary) {
  expanded.value = expanded.value === item.id ? '' : item.id
  if (expanded.value && !details.value[item.id]) details.value[item.id] = await api.getReport(item.id)
}
async function remove(item: ReportSummary) {
  try {
    await ElMessageBox.confirm(`永久删除“${item.title}”及所有报告版本？此操作不可恢复。`, '删除报告', { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' })
    await api.deleteReport(item.id)
    delete details.value[item.id]
    expanded.value = ''
    await load()
  } catch (reason) {
    if (reason !== 'cancel' && reason !== 'close') ElMessage.error(reason instanceof Error ? reason.message : '删除失败')
  }
}
function download(reportId: string, versionId: string) { window.location.href = `/api/v1/reports/${reportId}/versions/${versionId}/download` }
function openVersion(reportId: string, versionId: string) { window.location.href = `/reports/${reportId}/versions/${versionId}` }
function schemaLabel(version: ReportDetail['versions'][number]) {
  return version.content.analysis.report_schema_version === 2 ? 'v2' : `v${version.version_number}`
}
function formatTime(value: string) { return new Date(value).toLocaleString('zh-CN', {hour12: false}) }
</script>

<template>
  <main class="workspace library-page">
    <header class="topbar"><div><h1>报告库</h1><p>已验证分析的持久化版本</p></div><button class="button" :disabled="loading" @click="load"><RefreshCw :size="16" />刷新</button></header>
    <section class="library-content">
      <div v-if="items.length" class="asset-table report-table">
        <div class="asset-row asset-head"><span>报告</span><span>最新版本</span><span>状态</span><span>更新时间</span><span></span></div>
        <template v-for="item in items" :key="item.id">
          <div class="asset-row"><button class="asset-name" @click="toggle(item)"><component :is="expanded === item.id ? ChevronDown : ChevronRight" :size="16" /><FileText :size="16" /><span><strong>{{ item.title }}</strong><small>{{ item.id.slice(0, 8) }}</small></span></button><strong>{{ details[item.id]?.versions[0] ? schemaLabel(details[item.id].versions[0]) : '加载中…' }}</strong><span class="status-pill completed">已发布</span><span>{{ formatTime(item.updated_at) }}</span><div class="row-actions"><button class="icon-button" title="删除报告" aria-label="删除报告" @click="remove(item)"><Trash2 :size="16" /></button><button class="button open-report" :disabled="!details[item.id]?.versions[0]" @click="details[item.id]?.versions[0] && openVersion(item.id, details[item.id].versions[0].id)"><FileText :size="15" />打开报告</button></div></div>
          <div v-if="expanded === item.id" class="revision-list"><div v-for="version in details[item.id]?.versions" :key="version.id" class="revision-row report-version"><strong>{{ schemaLabel(version) }}</strong><span>{{ formatTime(version.created_at) }}</span><span>数据版本 {{ version.content.provenance.data_revision }}</span><code>{{ version.content_hash.slice(0, 12) }}</code><div class="revision-actions"><button class="button" @click="openVersion(item.id, version.id)"><FileText :size="15" />查看</button><button class="button" @click="download(item.id, version.id)"><Download :size="15" />下载</button></div></div></div>
        </template>
      </div>
      <div v-else-if="!loading" class="history-empty"><FileText :size="28" /><p>在分析结果页发布后，报告会保存在这里。</p></div>
    </section>
  </main>
</template>
