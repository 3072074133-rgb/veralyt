<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Trash2, ChevronDown, ChevronRight, Database, Play, RefreshCw, Upload } from 'lucide-vue-next'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import { useRouter } from 'vue-router'
import { api } from '../api'
import type { DatasetAsset, DatasetAssetDetail } from '../types'

const items = ref<DatasetAsset[]>([])
const detail = ref<Record<string, DatasetAssetDetail>>({})
const expanded = ref('')
const loading = ref(false)
const uploading = ref(false)
const fileInput = ref<HTMLInputElement>()
const router = useRouter()

onMounted(load)
async function load() {
  loading.value = true
  try { items.value = (await api.listDatasets()).items }
  catch (reason) { ElMessage.error(reason instanceof Error ? reason.message : '数据集加载失败') }
  finally { loading.value = false }
}
async function toggle(item: DatasetAsset) {
  expanded.value = expanded.value === item.id ? '' : item.id
  if (expanded.value && !detail.value[item.id]) {
    try { detail.value[item.id] = await api.getDataset(item.id) }
    catch (reason) { ElMessage.error(reason instanceof Error ? reason.message : '版本记录加载失败') }
  }
}
async function remove(item: DatasetAsset) {
  try {
    await ElMessageBox.confirm(`永久删除“${item.name}”及所有版本？此操作不可恢复。`, '删除数据集', {type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消'})
    await api.deleteDataset(item.id)
    await load()
  } catch (reason) {
    if (reason !== 'cancel' && reason !== 'close') ElMessage.error(reason instanceof Error ? reason.message : '删除失败')
  }
}
function formatTime(value: string) { return new Date(value).toLocaleString('zh-CN', {hour12: false}) }
async function analyze(datasetId: string, revisionId: string) {
  try {
    const task = await api.createTaskFromDataset(datasetId, revisionId)
    await router.push(`/tasks/${task.id}`)
  } catch (reason) { ElMessage.error(reason instanceof Error ? reason.message : '创建分析任务失败') }
}
async function uploadDatasets(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  if (!files.length) return
  uploading.value = true
  try {
    const result = await api.uploadDatasetFiles(files)
    if (result.accepted_files.length) {
      ElMessage.success(`已发布 ${result.accepted_files.length} 个数据集`)
      await load()
    }
    if (result.rejected_files.length) {
      ElMessage.warning(result.rejected_files.map((item) => `${item.name}：${item.message}`).join('；'))
    }
  } catch (reason) {
    ElMessage.error(reason instanceof Error ? reason.message : '数据集上传失败')
  } finally {
    uploading.value = false
    input.value = ''
  }
}
</script>

<template>
  <main class="workspace library-page">
    <header class="topbar"><div><h1>数据集</h1><p>可复用的数据资产与不可变版本</p></div><div class="top-actions"><input ref="fileInput" hidden type="file" accept=".xlsx,.csv" multiple @change="uploadDatasets"><button class="button primary" :disabled="uploading" @click="fileInput?.click()"><Upload :size="16" />{{ uploading ? '上传中' : '上传数据集' }}</button><button class="button" :disabled="loading" @click="load"><RefreshCw :size="16" />刷新</button></div></header>
    <section class="library-content">
      <div class="library-summary"><div><strong>{{ items.length }}</strong><span>个活动数据集</span></div><p>任务不会自动切换到新版本，历史分析始终保留原始数据指纹。</p></div>
      <div v-if="items.length" class="asset-table">
        <div class="asset-row asset-head"><span>名称</span><span>当前版本</span><span>所有者</span><span>更新时间</span><span></span></div>
        <template v-for="item in items" :key="item.id">
          <div class="asset-row"><button class="asset-name" @click="toggle(item)"><component :is="expanded === item.id ? ChevronDown : ChevronRight" :size="16" /><Database :size="16" /><span><strong>{{ item.name }}</strong><small>{{ item.description || '本地数据集' }}</small></span></button><strong>v{{ item.latest_revision }}</strong><span>{{ item.owner_id === 'local' ? '本地工作区' : item.owner_id }}</span><span>{{ formatTime(item.updated_at) }}</span><button class="icon-button" title="删除" @click="remove(item)"><Trash2 :size="16" /></button></div>
          <div v-if="expanded === item.id" class="revision-list"><div v-for="revision in detail[item.id]?.revisions" :key="revision.id" class="revision-row"><strong>v{{ revision.revision_number }}</strong><span>{{ revision.change_summary }}</span><span>{{ revision.tables.reduce((sum, table) => sum + table.row_count, 0).toLocaleString() }} 行</span><code>{{ revision.content_hash.slice(0, 12) }}</code><span>{{ formatTime(revision.created_at) }}</span><button class="button" @click="analyze(item.id, revision.id)"><Play :size="14" />分析</button></div></div>
        </template>
      </div>
      <div v-else-if="!loading" class="history-empty"><Database :size="28" /><p>上传文件后，数据集会出现在这里。</p></div>
    </section>
  </main>
</template>
