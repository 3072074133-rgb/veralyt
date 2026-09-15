<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { AllCommunityModule, ModuleRegistry, type CellValueChangedEvent, type ColDef, type GridApi, type GridReadyEvent } from 'ag-grid-community'
import { AgGridVue } from 'ag-grid-vue3'
import 'ag-grid-community/styles/ag-grid.css'
import 'ag-grid-community/styles/ag-theme-quartz.css'
import { ChevronLeft, ChevronRight, Plus, Save, Trash2, X, Pencil } from 'lucide-vue-next'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import type { DatasetCorrectionRequest, DatasetInfo, DatasetPreview, DatasetProfile } from '../types'

ModuleRegistry.registerModules([AllCommunityModule])

const props = defineProps<{ open: boolean; taskId?: string; datasets: DatasetInfo[]; initialDatasetId?: string; running?: boolean }>()
const emit = defineEmits<{ close: []; published: [] }>()
const selectedId = ref('')
const preview = ref<DatasetPreview>()
const profile = ref<DatasetProfile>()
const tab = ref<'preview' | 'columns' | 'profile'>('preview')
const loading = ref(false)
const saving = ref(false)
const page = ref(1)
const pageSize = 50
const gridApi = ref<GridApi>()
const cellUpdates = ref(new Map<string, {row_id: number; column: string; value: unknown}>())
const deletedRowIds = ref<number[]>([])
const addedRows = ref<Array<Record<string, unknown> & {__local_id: string}>>([])
const metadata = ref<DatasetInfo['columns']>([])
const initialMetadata = ref('')
const changeSummary = ref('数据纠错')

const selectedDataset = computed(() => props.datasets.find((item) => item.id === selectedId.value))
const totalPages = computed(() => Math.max(1, Math.ceil((preview.value?.total ?? 0) / pageSize)))
const rowData = computed(() => [...(preview.value?.rows ?? []).filter((row) => !deletedRowIds.value.includes(row.__row_id)), ...addedRows.value])
const columnDefs = computed<ColDef[]>(() => [
  { field: '__row_id', headerName: '行号', width: 82, pinned: 'left', editable: false, valueGetter: (params: {data?: Record<string, unknown>}) => params.data?.__row_id ?? '新' },
  ...(preview.value?.columns ?? []).map((column) => ({
    field: column.name,
    headerName: column.display_name,
    editable: !props.running && !saving.value,
    cellEditor: 'agTextCellEditor',
    cellDataType: false,
    sortable: true,
    filter: true,
    resizable: true,
    minWidth: 130,
  })),
])
const dirtyCount = computed(() => cellUpdates.value.size + deletedRowIds.value.length + addedRows.value.length + metadataChangeCount.value)
const metadataChangeCount = computed(() => {
  if (!initialMetadata.value) return 0
  const initial = JSON.parse(initialMetadata.value) as DatasetInfo['columns']
  return metadata.value.filter((item, index) => JSON.stringify(item) !== JSON.stringify(initial[index])).length
})

watch(() => props.open, async (open) => {
  if (!open) return
  const requestedId = props.initialDatasetId && props.datasets.some((item) => item.id === props.initialDatasetId) ? props.initialDatasetId : ''
  const nextId = requestedId || (selectedId.value && props.datasets.some((item) => item.id === selectedId.value)
    ? selectedId.value : props.datasets[0]?.id ?? '')
  page.value = 1
  if (selectedId.value !== nextId) { selectedId.value = nextId; return }
  await loadAll()
  await nextTick()
})
watch(selectedId, async () => { if (props.open) { page.value = 1; await loadAll() } })

async function loadAll() {
  if (!props.taskId || !selectedId.value) return
  loading.value = true
  try {
    const [nextPreview, nextProfile] = await Promise.all([
      api.getDatasetPreview(props.taskId, selectedId.value, page.value, pageSize),
      api.getDatasetProfile(props.taskId, selectedId.value),
    ])
    preview.value = nextPreview
    profile.value = nextProfile
    metadata.value = nextPreview.columns.map((item) => ({...item}))
    initialMetadata.value = JSON.stringify(metadata.value)
    cellUpdates.value.clear()
    deletedRowIds.value = []
    addedRows.value = []
  } catch (reason) {
    ElMessage.error(reason instanceof Error ? reason.message : '数据加载失败')
  } finally { loading.value = false }
}

async function movePage(direction: number) {
  const target = page.value + direction
  if (target < 1 || target > totalPages.value || dirtyCount.value) return
  page.value = target
  if (!props.taskId) return
  loading.value = true
  try { preview.value = await api.getDatasetPreview(props.taskId, selectedId.value, page.value, pageSize) }
  catch (reason) { ElMessage.error(reason instanceof Error ? reason.message : '分页加载失败') }
  finally { loading.value = false }
}

function onGridReady(event: GridReadyEvent) { gridApi.value = event.api }
function onCellChanged(event: CellValueChangedEvent) {
  const row = event.data as Record<string, unknown> & {__row_id?: number; __local_id?: string}
  const column = event.colDef.field
  if (!column || column === '__row_id') return
  if (row.__local_id) {
    const target = addedRows.value.find((item) => item.__local_id === row.__local_id)
    if (target) target[column] = event.newValue
    return
  }
  if (row.__row_id) {
    const key = `${row.__row_id}:${column}`
    cellUpdates.value.set(key, { row_id: row.__row_id, column, value: event.newValue })
  }
}
function editSelected() {
  const api = gridApi.value
  if (!api || props.running || saving.value) return
  const focus = api.getFocusedCell()
  const rowIndex = api.getSelectedNodes()[0]?.rowIndex ?? focus?.rowIndex ?? 0
  const colKey = focus?.column.getColId() !== '__row_id' ? focus?.column.getColId() : undefined
  const field = colKey ?? preview.value?.columns[0]?.name
  if (!field || !api.getDisplayedRowAtIndex(rowIndex)) return
  api.ensureIndexVisible(rowIndex)
  api.setFocusedCell(rowIndex, field)
  api.startEditingCell({ rowIndex, colKey: field })
}
function addRow() {
  const row = Object.fromEntries((preview.value?.columns ?? []).map((column) => [column.name, null])) as Record<string, unknown> & {__local_id: string}
  row.__local_id = crypto.randomUUID()
  addedRows.value.push(row)
}
function deleteRows() {
  const selected = gridApi.value?.getSelectedRows() as Array<Record<string, unknown> & {__row_id?: number; __local_id?: string}> ?? []
  if (!selected.length) return
  deletedRowIds.value = [...new Set([...deletedRowIds.value, ...selected.flatMap((row) => row.__row_id ? [row.__row_id] : [])])]
  const removedLocal = new Set(selected.flatMap((row) => row.__local_id ? [row.__local_id] : []))
  addedRows.value = addedRows.value.filter((row) => !removedLocal.has(row.__local_id))
}

async function publish() {
  gridApi.value?.stopEditing()
  if (!props.taskId || !preview.value || !dirtyCount.value) return
  saving.value = true
  try {
    const initial = JSON.parse(initialMetadata.value) as DatasetInfo['columns']
    const metadataUpdates: DatasetCorrectionRequest['metadata_updates'] = []
    metadata.value.forEach((item, index) => {
      if (JSON.stringify(item) === JSON.stringify(initial[index])) return
      metadataUpdates.push({
        column: item.name,
        display_name: item.display_name,
        semantic_type: item.semantic_type,
        role: item.role,
        unit: item.unit ?? '',
        currency: item.currency ?? '',
        default_aggregation: item.default_aggregation,
      })
    })
    const result = await api.correctDataset(props.taskId, selectedId.value, {
      expected_data_revision: preview.value.data_revision,
      cell_updates: [...cellUpdates.value.values()],
      metadata_updates: metadataUpdates,
      deleted_row_ids: deletedRowIds.value,
      added_rows: addedRows.value.map(({__local_id: _, ...row}) => row),
      change_summary: changeSummary.value.trim() || '数据纠错',
    })
    ElMessage.success(`修订 v${result.revision_number} 已发布`)
    emit('published')
    await loadAll()
  } catch (reason) {
    ElMessage.error(reason instanceof Error ? reason.message : '修订发布失败')
  } finally { saving.value = false }
}
</script>

<template>
  <div v-if="open" class="dataset-workspace-backdrop">
    <section class="dataset-workspace" role="dialog" aria-modal="true" aria-labelledby="dataset-workspace-title">
      <header>
        <div><h2 id="dataset-workspace-title">数据预览与纠错</h2><p>修改会发布为新版本，已有版本和证据不会被覆盖。</p></div>
        <div class="row-actions">
          <span v-if="dirtyCount" class="dirty-count">{{ dirtyCount }} 项待发布</span>
          <button class="button primary" :disabled="!dirtyCount || saving || running" @click="publish"><Save :size="16" />发布修订</button>
          <button class="icon-button" title="关闭" @click="emit('close')"><X :size="19" /></button>
        </div>
      </header>
      <div class="dataset-toolbar">
        <label><span>数据表</span><select v-model="selectedId" :disabled="dirtyCount > 0"><option v-for="item in datasets" :key="item.id" :value="item.id">{{ item.display_name }}</option></select></label>
        <label class="change-summary"><span>版本说明</span><input v-model="changeSummary" maxlength="200"></label>
        <div class="dataset-tabs" role="tablist"><button v-for="item in [['preview','数据'],['columns','字段'],['profile','质量']]" :key="item[0]" :class="{active: tab === item[0]}" @click="tab = item[0] as typeof tab">{{ item[1] }}</button></div>
      </div>
      <div v-if="tab === 'preview'" class="dataset-preview-pane">
        <div class="dataset-grid-actions"><span>{{ preview?.total.toLocaleString() ?? 0 }} 行 · 第 {{ page }}/{{ totalPages }} 页</span><div><button class="button" :disabled="running || saving || loading || !rowData.length" @click="editSelected"><Pencil :size="15" />编辑所选</button><button class="button" :disabled="running || saving" @click="addRow"><Plus :size="15" />新增行</button><button class="button" :disabled="running || saving" @click="deleteRows"><Trash2 :size="15" />删除所选</button></div></div>
        <AgGridVue class="ag-theme-quartz correction-grid" theme="legacy" :loading="loading" :column-defs="columnDefs" :row-data="rowData" :row-selection="{mode: 'multiRow'}" :single-click-edit="true" :stop-editing-when-cells-lose-focus="true" :undo-redo-cell-editing="true" @grid-ready="onGridReady" @cell-value-changed="onCellChanged" />
        <footer><button class="icon-button" title="上一页" :disabled="page <= 1 || dirtyCount > 0" @click="movePage(-1)"><ChevronLeft :size="18" /></button><span>{{ page }} / {{ totalPages }}</span><button class="icon-button" title="下一页" :disabled="page >= totalPages || dirtyCount > 0" @click="movePage(1)"><ChevronRight :size="18" /></button></footer>
      </div>
      <div v-else-if="tab === 'columns'" class="column-editor">
        <div class="column-row column-head"><span>字段</span><span>显示名称</span><span>语义类型</span><span>角色</span><span>单位</span><span>默认聚合</span></div>
        <div v-for="column in metadata" :key="column.name" class="column-row"><code>{{ column.name }}</code><input v-model="column.display_name"><select v-model="column.semantic_type"><option v-for="value in ['id','name','date','amount','percentage','category','metric','unknown']" :key="value">{{ value }}</option></select><select v-model="column.role"><option v-for="value in ['dimension','measure','identifier','unknown']" :key="value">{{ value }}</option></select><input v-model="column.unit" placeholder="无"><select v-model="column.default_aggregation"><option v-for="value in ['sum','average','count','none']" :key="value">{{ value }}</option></select></div>
      </div>
      <div v-else class="profile-pane">
        <div class="quality-summary"><article><small>总行数</small><strong>{{ profile?.row_count.toLocaleString() }}</strong></article><article><small>重复行</small><strong>{{ profile?.duplicate_count.toLocaleString() }}</strong></article><article><small>字段数</small><strong>{{ profile?.columns.length }}</strong></article></div>
        <div class="profile-table"><div class="profile-row profile-head"><span>字段</span><span>类型</span><span>缺失</span><span>唯一值</span><span>范围 / 样例</span></div><div v-for="column in profile?.columns" :key="column.name" class="profile-row"><strong>{{ column.display_name }}</strong><code>{{ column.data_type }}</code><span>{{ column.null_count }} ({{ (column.null_ratio * 100).toFixed(1) }}%)</span><span>{{ column.distinct_count }}</span><span>{{ column.minimum ?? column.sample_values[0] ?? '-' }}<template v-if="column.maximum !== undefined"> 至 {{ column.maximum }}</template></span></div></div>
      </div>
    </section>
  </div>
</template>
