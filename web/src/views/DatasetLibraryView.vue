<script setup lang="ts">
import { defineAsyncComponent, onMounted, onBeforeUnmount, ref } from 'vue'
import { Trash2, ChevronDown, ChevronRight, Database, Play, RefreshCw, Upload, Pencil } from 'lucide-vue-next'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import { useRouter } from 'vue-router'
import { api } from '../api'
import type { DatasetAsset, DatasetAssetDetail, TaskSnapshot } from '../types'

const DatasetWorkspace = defineAsyncComponent(() => import('../components/DatasetWorkspace.vue'))
const editorTask = ref<TaskSnapshot>()
const editorOpen = ref(false)
const editing = ref(false)
const editingAsset = ref('')
async function editDataset(item: DatasetAsset) {
  if (editing.value) return
  editing.value = true
  try {
    const asset = await api.getDataset(item.id)
    const revision = asset.revisions.find(version => version.revision_number === item.latest_revision)
    if (!revision) throw new Error('未找到当前版本')
    const task = await api.createDatasetEditor(item.id, revision.id)
    editorTask.value = await api.getTask(task.id)
    editingAsset.value = item.id
    editorOpen.value = true
  } catch (reason) { ElMessage.error(reason instanceof Error ? reason.message : '打开编辑失败') }
  finally { editing.value = false }
}
async function refreshRevision() {
  if (editorTask.value) editorTask.value = await api.getTask(editorTask.value.id)
  detail.value[editingAsset.value] = await api.getDataset(editingAsset.value)
  await load()
}
function closeEditor() {
  editorOpen.value = false
  const id = editorTask.value?.id
  editorTask.value = undefined
  if (id) void api.deleteTask(id).catch(() => { /* Editor contexts remain excluded from history if cleanup fails. */ })
}
onBeforeUnmount(closeEditor)

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
async function removeRevision(item: DatasetAsset, revisionId: string, version: number) {
  try {
    await ElMessageBox.confirm(`删除“${item.name}”的 v${version}？历史分析所需的数据快照会保留。`, '删除版本', { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' })
    await api.deleteDatasetRevision(item.id, revisionId)
    detail.value[item.id] = await api.getDataset(item.id)
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
          <div class="asset-row"><button class="asset-name" @click="toggle(item)"><component :is="expanded === item.id ? ChevronDown : ChevronRight" :size="16" /><Database :size="16" /><span><strong>{{ item.name }}</strong><small>{{ item.description || '本地数据集' }}</small></span></button><strong>v{{ item.latest_revision }}</strong><span>{{ item.owner_id === 'local' ? '本地工作区' : item.owner_id }}</span><span>{{ formatTime(item.updated_at) }}</span><div class="row-actions"><button class="button" :disabled="editing" @click="editDataset(item)"><Pencil :size="15" />修改</button><button class="icon-button" title="删除" @click="remove(item)"><Trash2 :size="16" /></button></div></div>
          <div v-if="expanded === item.id" class="revision-list"><div v-for="revision in detail[item.id]?.revisions" :key="revision.id" class="revision-row"><strong>v{{ revision.revision_number }}</strong><span>{{ revision.change_summary }}</span><span>{{ revision.tables.reduce((sum, table) => sum + table.row_count, 0).toLocaleString() }} 行</span><code>{{ revision.content_hash.slice(0, 12) }}</code><span>{{ formatTime(revision.created_at) }}</span><div class="row-actions"><button class="button" @click="analyze(item.id, revision.id)"><Play :size="14" />分析</button><button class="icon-button" :title="`删除 v${revision.revision_number}`" :aria-label="`删除 v${revision.revision_number}`" :disabled="detail[item.id]?.revisions.length <= 1" @click="removeRevision(item, revision.id, revision.revision_number)"><Trash2 :size="15" /></button></div></div></div>
        </template>
      </div>
      <div v-else-if="!loading" class="history-empty"><Database :size="28" /><p>上传文件后，数据集会出现在这里。</p></div>
    </section>
    <DatasetWorkspace :open="editorOpen" :task-id="editorTask?.id" :datasets="editorTask?.datasets ?? []" @close="closeEditor" @published="refreshRevision" />
  </main>
</template>

<style scoped>
.asset-row { grid-template-columns: minmax(220px, 1.5fr) 90px 110px 165px 125px; }
.revision-row { grid-template-columns: 54px minmax(150px, 1fr) 90px 110px 165px 120px; }
</style>
