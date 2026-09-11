<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { ChevronLeft, ChevronRight, RefreshCw, Search, Trash2 } from 'lucide-vue-next'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import { useRouter } from 'vue-router'
import { api } from '../api'
import type { TaskListItem } from '../types'

const router = useRouter()
const tasks = ref<TaskListItem[]>([])
const query = ref('')
const status = ref('')
const loading = ref(false)
const page = ref(1)
const pageSize = 20
const total = ref(0)
let timer: number | undefined

onMounted(load)
watch([query, status], () => { page.value = 1; window.clearTimeout(timer); timer = window.setTimeout(load, 250) })

async function load() {
  loading.value = true
  try {
    const result = await api.listTasks(query.value, status.value, page.value, pageSize)
    tasks.value = result.items
    total.value = result.total
  } catch (reason) {
    ElMessage.error(reason instanceof Error ? reason.message : '历史任务加载失败')
  }
  finally { loading.value = false }
}
async function remove(task: TaskListItem) {
  if (!window.confirm(`删除“${task.title}”及其本地数据？此操作无法撤销。`)) return
  try { await api.deleteTask(task.id); await load() }
  catch (reason) { ElMessage.error(reason instanceof Error ? reason.message : '删除失败') }
}
async function changePage(value: number) { page.value = value; await load() }
const labels: Record<string, string> = { ready: '待分析', ingesting: '读取中', classifying: '识别需求', planning: '规划中', needs_clarification: '待确认', executing: '分析中', validating: '校验中', reflecting: '复核中', needs_review: '待人工复核', completed_with_warnings: '完成但有警告', completed: '已完成', failed: '失败', off_topic: '非分析问题' }
</script>

<template>
  <main class="workspace">
    <header class="topbar"><div><h1>历史分析任务</h1><p>保存在这台电脑上的分析记录</p></div><button class="button" :disabled="loading" @click="load"><RefreshCw :size="16" />刷新</button></header>
    <section class="history-content">
      <div class="history-filters"><label><Search :size="16" /><input v-model="query" placeholder="搜索任务名称或数据文件"></label><select v-model="status"><option value="">全部状态</option><option value="completed">已完成</option><option value="completed_with_warnings">完成但有警告</option><option value="needs_review">待人工复核</option><option value="failed">失败</option><option value="needs_clarification">待确认</option><option value="ready">待分析</option></select><button class="button" @click="query='';status=''">清除筛选</button></div>
      <p class="history-summary">共 {{ total }} 个任务</p>
      <div class="history-table">
        <div class="history-row history-head"><span>任务</span><span>状态</span><span>更新时间</span><span>数据文件</span><span>操作</span></div>
        <div v-for="task in tasks" :key="task.id" class="history-row">
          <div class="task-title"><strong>{{ task.title }}</strong><small>{{ task.status_message }}</small></div>
          <span :class="['status-pill', task.status]">{{ labels[task.status] }}</span>
          <span>{{ new Date(task.updated_at).toLocaleString('zh-CN', {hour12:false}) }}</span>
          <div class="task-files"><strong>{{ task.file_names[0] || '暂无文件' }}</strong><small v-if="task.file_names.length > 1">另有 {{ task.file_names.length - 1 }} 个文件</small></div>
          <div class="row-actions"><button class="open-button" @click="router.push(`/tasks/${task.id}`)">打开</button><button class="icon-button danger-text" title="删除任务" @click="remove(task)"><Trash2 :size="16" /></button></div>
        </div>
        <div v-if="!tasks.length && !loading" class="history-empty">没有符合条件的历史任务</div>
      </div>
      <nav v-if="total > pageSize" class="history-pagination" aria-label="历史任务分页">
        <button class="icon-button" title="上一页" :disabled="page === 1 || loading" @click="changePage(page - 1)"><ChevronLeft :size="17" /></button>
        <span>第 {{ page }} / {{ Math.ceil(total / pageSize) }} 页</span>
        <button class="icon-button" title="下一页" :disabled="page >= Math.ceil(total / pageSize) || loading" @click="changePage(page + 1)"><ChevronRight :size="17" /></button>
      </nav>
    </section>
  </main>
</template>
