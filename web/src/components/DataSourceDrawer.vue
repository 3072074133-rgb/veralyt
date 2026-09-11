<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { AlertCircle, Database, FilePlus2, FileSpreadsheet, Table2, X } from 'lucide-vue-next'
import type { DatasetInfo, UploadedFile, UploadFailure } from '../types'

const props = defineProps<{
  open: boolean
  files: UploadedFile[]
  datasets: DatasetInfo[]
  uploadFailures: UploadFailure[]
  running?: boolean
}>()
const emit = defineEmits<{
  close: []
  addFiles: []
  openWorkspace: [datasetId?: string]
}>()

const drawer = ref<HTMLElement>()
const tab = ref<'files' | 'datasets'>('files')
let previousFocus: HTMLElement | null = null
let previousOverflow = ''

const totalRows = computed(() => props.files.reduce((sum, file) => sum + file.row_count, 0))
const failedCount = computed(() => props.files.filter((file) => file.status === 'failed').length + props.uploadFailures.length)

watch(() => props.open, async (open) => {
  if (open) {
    previousFocus = document.activeElement as HTMLElement
    previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    tab.value = failedCount.value ? 'files' : tab.value
    await nextTick()
    drawer.value?.focus()
    return
  }
  restorePage()
}, { immediate: true })

onBeforeUnmount(restorePage)

function restorePage() {
  document.body.style.overflow = previousOverflow
  previousFocus?.focus()
  previousFocus = null
}

function formatSize(size: number) {
  return size > 1048576 ? `${(size / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(size / 1024))} KB`
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    emit('close')
    return
  }
  if (event.key !== 'Tab' || !drawer.value) return
  const focusable = [...drawer.value.querySelectorAll<HTMLElement>('button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])')]
  if (!focusable.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

function openWorkspace(datasetId?: string) {
  emit('openWorkspace', datasetId)
}
</script>

<template>
  <div v-if="open" class="source-drawer-backdrop" @click.self="emit('close')">
    <aside ref="drawer" class="source-drawer" role="dialog" aria-modal="true" aria-labelledby="source-drawer-title" tabindex="-1" @keydown="onKeydown">
      <header>
        <div class="source-drawer-heading">
          <span class="source-drawer-icon"><Database :size="18" /></span>
          <div><h2 id="source-drawer-title">数据来源</h2><p>{{ files.length }} 个文件 · {{ datasets.length }} 个数据表 · {{ totalRows.toLocaleString() }} 行</p></div>
        </div>
        <button class="icon-button" title="关闭数据来源" aria-label="关闭数据来源" @click="emit('close')"><X :size="19" /></button>
      </header>

      <div v-if="failedCount" class="source-drawer-warning" role="status">
        <AlertCircle :size="17" /><span>{{ failedCount }} 个文件未能完整导入，请检查下方原因。</span>
      </div>

      <div class="source-tabs" role="tablist" aria-label="数据来源视图">
        <button id="source-files-tab" role="tab" :aria-selected="tab === 'files'" :class="{ active: tab === 'files' }" @click="tab='files'">文件 <span>{{ files.length + uploadFailures.length }}</span></button>
        <button id="source-datasets-tab" role="tab" :aria-selected="tab === 'datasets'" :class="{ active: tab === 'datasets' }" @click="tab='datasets'">数据表 <span>{{ datasets.length }}</span></button>
      </div>

      <div class="source-drawer-body">
        <section v-if="tab === 'files'" role="tabpanel" aria-labelledby="source-files-tab" class="source-list">
          <article v-for="file in files" :key="file.id" class="source-file-card">
            <div class="source-file-title">
              <span class="file-badge"><FileSpreadsheet :size="16" /></span>
              <div><strong :title="file.original_name">{{ file.original_name }}</strong><small>{{ formatSize(file.size) }}</small></div>
              <span :class="['source-status', file.status]">{{ file.status === 'ready' ? '已就绪' : '失败' }}</span>
            </div>
            <dl>
              <div><dt>工作表</dt><dd>{{ file.sheet_count }}</dd></div>
              <div><dt>数据行</dt><dd>{{ file.row_count.toLocaleString() }}</dd></div>
              <div><dt>检测到</dt><dd>{{ file.detected_sheet_count || file.sheet_count }}</dd></div>
            </dl>
            <p v-if="file.status === 'ready' && file.skipped_sheet_count" class="source-note warning">已跳过 {{ file.skipped_sheet_count }} 个隐藏或空白工作表。</p>
            <p v-else-if="file.error" class="source-note error">{{ file.error }}</p>
          </article>

          <article v-for="item in uploadFailures" :key="item.name" class="source-file-card failed-import">
            <div class="source-file-title">
              <span class="file-badge failed"><FileSpreadsheet :size="16" /></span>
              <div><strong :title="item.name">{{ item.name }}</strong><small>未导入</small></div>
              <span class="source-status failed">失败</span>
            </div>
            <p class="source-note error">{{ item.message }}</p>
          </article>

          <div v-if="!files.length && !uploadFailures.length" class="source-empty"><FileSpreadsheet :size="24" /><p>当前分析还没有数据文件</p></div>
        </section>

        <section v-else role="tabpanel" aria-labelledby="source-datasets-tab" class="source-list">
          <article v-for="dataset in datasets" :key="dataset.id" class="source-dataset-row">
            <span class="source-table-icon"><Table2 :size="16" /></span>
            <div>
              <strong :title="dataset.display_name">{{ dataset.display_name }}</strong>
              <small>{{ dataset.row_count.toLocaleString() }} 行 · {{ dataset.columns.length }} 个字段<span v-if="dataset.source_region"> · {{ dataset.source_region.source_range }}</span></small>
            </div>
            <button class="button" :aria-label="`预览 ${dataset.display_name}`" @click="openWorkspace(dataset.id)">预览</button>
          </article>
          <div v-if="!datasets.length" class="source-empty"><Table2 :size="24" /><p>暂无可预览的数据表</p></div>
        </section>
      </div>

      <footer>
        <button class="button" :disabled="running" @click="emit('addFiles')"><FilePlus2 :size="16" />添加文件</button>
        <button class="button primary" :disabled="!datasets.length" @click="openWorkspace()"><Database :size="16" />预览与纠错</button>
      </footer>
    </aside>
  </div>
</template>
