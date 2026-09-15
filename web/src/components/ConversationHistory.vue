<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Search, RefreshCw, Trash2 } from 'lucide-vue-next'
import { api } from '../api'
import { useTaskStore } from '../stores/task'
import type { TaskListItem } from '../types'

const route = useRoute()
const router = useRouter()
const store = useTaskStore()
const tasks = ref<TaskListItem[]>([])
const query = ref('')
const page = ref(1)
const total = ref(0)
const loading = ref(false)
const error = ref('')
let generation = 0
let timer: ReturnType<typeof setTimeout> | undefined
const groups = computed(() => {
  const today = new Date(); today.setHours(0, 0, 0, 0)
  const yesterday = new Date(today); yesterday.setDate(today.getDate() - 1)
  const week = new Date(today); week.setDate(today.getDate() - 7)
  const result: Record<string, TaskListItem[]> = {}
  for (const task of tasks.value) {
    const date = new Date(task.updated_at)
    const label = date >= today ? '今天' : date >= yesterday ? '昨天' : date >= week ? '最近 7 天' : '更早'
    ;(result[label] ??= []).push(task)
  }
  return result
})
async function load(more = false) {
  const token = ++generation
  const nextPage = more ? page.value + 1 : 1
  loading.value = true; error.value = ''
  try {
    const result = await api.listTasks(query.value, '', nextPage, 30)
    if (token !== generation) return
    tasks.value = more ? [...new Map([...tasks.value, ...result.items].map(task => [task.id, task])).values()] : result.items
    total.value = result.total; page.value = nextPage
  } catch (reason) {
    if (token === generation) error.value = reason instanceof Error ? reason.message : '会话加载失败'
  } finally { if (token === generation) loading.value = false }
}
function scheduleLoad() {
  generation++
  clearTimeout(timer)
  timer = setTimeout(() => void load(), 250)
}
watch(query, scheduleLoad)
watch(() => route.fullPath, scheduleLoad)
watch(() => [store.task?.id, store.task?.title, store.task?.status], scheduleLoad)
onMounted(() => void load())
onBeforeUnmount(() => { generation++; clearTimeout(timer) })
async function remove(task: TaskListItem) {
  if (!window.confirm(`删除“${task.title}”及其本地数据？此操作无法撤销。`)) return
  try {
    await api.deleteTask(task.id)
    if (store.currentTaskId === task.id) store.startNewAnalysis()
    if (route.params.id === task.id) await router.replace('/')
    await load()
  } catch (reason) { error.value = reason instanceof Error ? reason.message : '删除失败' }
}
</script>

<template>
  <section class="sidebar-history" aria-label="历史会话">
    <header><strong>历史会话</strong><button class="icon-button" title="刷新会话" :disabled="loading" @click="load()"><RefreshCw :size="14" /></button></header>
    <label class="conversation-search"><Search :size="14" /><input v-model="query" aria-label="搜索历史会话" placeholder="搜索会话或文件" /></label>
    <div class="conversation-list" :aria-busy="loading">
      <p v-if="error" role="alert" class="history-note">{{ error }}</p>
      <template v-for="(items, label) in groups" :key="label">
        <h3>{{ label }}</h3>
        <div v-for="task in items" :key="task.id" class="conversation-item" :class="{ selected: route.params.id === task.id }">
          <router-link :to="`/tasks/${task.id}`" :title="`${task.title} · ${task.status_message}`" :aria-current="route.params.id === task.id ? 'page' : undefined"><span>{{ task.title }}</span><small>{{ task.file_names[0] || task.status_message }}</small></router-link>
          <button class="icon-button" :title="`删除会话：${task.title}`" @click="remove(task)"><Trash2 :size="13" /></button>
        </div>
      </template>
      <p v-if="!tasks.length && !error" class="history-note">{{ loading ? '加载中…' : query ? '没有匹配的会话' : '暂无历史会话' }}</p>
      <button v-if="tasks.length < total" class="history-more" :disabled="loading" @click="load(true)">{{ loading ? '加载中…' : '加载更多' }}</button>
    </div>
  </section>
</template>
