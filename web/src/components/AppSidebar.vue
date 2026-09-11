<script setup lang="ts">
import { BarChart3, BookOpen, Clock3, Database, FileSpreadsheet, FileText, Plus, UserRound } from 'lucide-vue-next'
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useTaskStore } from '../stores/task'

const router = useRouter()
const store = useTaskStore()
const currentAnalysisPath = computed(() => store.currentTaskId ? `/tasks/${store.currentTaskId}` : '/')
async function newTask() {
  store.startNewAnalysis()
  await router.push({ path: '/', query: { new: String(Date.now()) } })
}
</script>

<template>
  <aside class="sidebar">
    <div class="brand"><div class="brand-mark">DA</div><strong>数据分析智能体</strong></div>
    <button class="new-task" type="button" @click="newTask"><Plus :size="17" />新建分析</button>
    <p class="side-label">工作区</p>
    <router-link class="nav-item" :to="currentAnalysisPath"><BarChart3 :size="17" />当前分析</router-link>
    <router-link class="nav-item" to="/history"><Clock3 :size="17" />历史任务</router-link>
    <p class="side-label">本地数据</p>
    <router-link class="nav-item" to="/datasets"><Database :size="17" />数据集</router-link>
    <router-link class="nav-item" to="/knowledge"><BookOpen :size="17" />知识库</router-link>
    <router-link class="nav-item" to="/reports"><FileText :size="17" />报告库</router-link>
    <div class="recent-file"><span class="file-badge"><FileSpreadsheet :size="15" /></span><span><strong>数据仅保存在本机</strong><small>支持 XLSX 与 CSV</small></span></div>
    <div class="account"><span class="avatar"><UserRound :size="15" /></span><span><strong>本地工作区</strong><small>Ollama · qwen3.5:4b</small></span></div>
  </aside>
</template>
