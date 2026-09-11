<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { BookOpen, Check, Search, X } from 'lucide-vue-next'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import type { KnowledgeBaseDetail, KnowledgeBaseSummary, TaskKnowledgeBinding } from '../types'

const props = defineProps<{ open: boolean; taskId?: string; bindings: TaskKnowledgeBinding[]; running?: boolean }>()
const emit = defineEmits<{ close: []; updated: [bindings: TaskKnowledgeBinding[]] }>()
const drawer = ref<HTMLElement>()
const items = ref<KnowledgeBaseSummary[]>([])
const details = ref<Record<string, KnowledgeBaseDetail>>({})
const selected = ref<Record<string, string>>({})
const query = ref('')
const loading = ref(false)
const saving = ref(false)
let previousFocus: HTMLElement | null = null

const filtered = computed(() => {
  const value = query.value.trim().toLocaleLowerCase()
  return value ? items.value.filter((item) => `${item.name} ${item.description}`.toLocaleLowerCase().includes(value)) : items.value
})

watch(() => props.open, async (open) => {
  if (!open || !props.taskId) return
  previousFocus = document.activeElement as HTMLElement
  selected.value = Object.fromEntries(props.bindings.map((item) => [item.knowledge_base_id, item.revision_id]))
  query.value = ''
  loading.value = true
  try {
    items.value = (await api.listKnowledgeBases()).items
    const loaded = await Promise.all(items.value.map((item) => api.getKnowledgeBase(item.id)))
    details.value = Object.fromEntries(loaded.map((item) => [item.id, item]))
  } catch (reason) { ElMessage.error(reason instanceof Error ? reason.message : '知识库加载失败') }
  finally { loading.value = false }
  await nextTick()
  drawer.value?.focus()
})

function toggle(item: KnowledgeBaseSummary) {
  if (selected.value[item.id]) delete selected.value[item.id]
  else selected.value[item.id] = details.value[item.id]?.revisions[0]?.id ?? ''
  selected.value = { ...selected.value }
}

async function save() {
  if (!props.taskId) return
  saving.value = true
  try {
    const bindings = await api.setTaskKnowledgeBases(
      props.taskId,
      Object.entries(selected.value).filter(([, revisionId]) => revisionId).map(([knowledge_base_id, revision_id]) => ({ knowledge_base_id, revision_id })),
    )
    emit('updated', bindings)
    emit('close')
    ElMessage.success(bindings.length ? `已关联 ${bindings.length} 个知识库` : '已清除知识库关联')
  } catch (reason) { ElMessage.error(reason instanceof Error ? reason.message : '知识库关联失败') }
  finally { saving.value = false }
}

function close() { emit('close'); nextTick(() => previousFocus?.focus()) }
function onKeydown(event: KeyboardEvent) { if (event.key === 'Escape' && !saving.value) close() }
</script>

<template>
  <div v-if="open" class="source-drawer-backdrop" @click.self="close">
    <aside ref="drawer" class="source-drawer knowledge-binding-drawer" role="dialog" aria-modal="true" aria-labelledby="knowledge-binding-title" tabindex="-1" @keydown="onKeydown">
      <header>
        <div class="source-drawer-heading"><span class="source-drawer-icon"><BookOpen :size="18" /></span><div><h2 id="knowledge-binding-title">任务知识库</h2><p>{{ Object.keys(selected).length }} 个已关联</p></div></div>
        <button class="icon-button" title="关闭" :disabled="saving" @click="close"><X :size="18" /></button>
      </header>
      <label class="knowledge-search"><Search :size="15" /><input v-model="query" placeholder="搜索知识库"></label>
      <div class="source-drawer-body">
        <div v-if="loading" class="source-empty"><p>正在加载</p></div>
        <div v-else-if="filtered.length" class="knowledge-binding-list">
          <article v-for="item in filtered" :key="item.id" :class="['knowledge-binding-row', { selected: !!selected[item.id] }]">
            <button class="knowledge-check" :aria-label="selected[item.id] ? `取消关联 ${item.name}` : `关联 ${item.name}`" @click="toggle(item)"><Check v-if="selected[item.id]" :size="14" /></button>
            <button class="knowledge-binding-main" @click="toggle(item)"><strong>{{ item.name }}</strong><small>{{ item.description || `${item.document_count} 份知识内容` }}</small></button>
            <select v-if="selected[item.id]" v-model="selected[item.id]" aria-label="知识库版本">
              <option v-for="revision in details[item.id]?.revisions" :key="revision.id" :value="revision.id">v{{ revision.revision_number }}</option>
            </select>
            <span v-else>v{{ item.latest_revision }}</span>
          </article>
        </div>
        <div v-else class="source-empty"><BookOpen :size="24" /><p>暂无可用知识库</p><router-link class="button" to="/knowledge" @click="close">新建知识库</router-link></div>
      </div>
      <footer><router-link class="button" to="/knowledge" @click="close"><BookOpen :size="15" />管理知识库</router-link><button class="button primary" :disabled="running || saving" @click="save">{{ saving ? '正在保存' : '应用到任务' }}</button></footer>
    </aside>
  </div>
</template>
