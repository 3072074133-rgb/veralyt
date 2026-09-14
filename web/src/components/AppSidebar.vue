<script setup lang="ts">
import { BarChart3, Clock3, Database, FileSpreadsheet, FileText, Plus, UserRound, PanelLeftClose, PanelLeftOpen } from 'lucide-vue-next'
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useTaskStore } from '../stores/task'

const router = useRouter()
defineProps<{ collapsed: boolean }>()
defineEmits<{ toggle: [] }>()
const store = useTaskStore()
const currentAnalysisPath = computed(() => store.currentTaskId ? `/tasks/${store.currentTaskId}` : '/')
async function newTask() {
  store.startNewAnalysis()
  await router.push({ path: '/', query: { new: String(Date.now()) } })
}
</script>

<template>
  <aside class="sidebar">
    <button class="icon-button sidebar-toggle" :title="collapsed ? '展开侧边栏' : '收起侧边栏'" :aria-label="collapsed ? '展开侧边栏' : '收起侧边栏'" :aria-expanded="!collapsed" @click="$emit('toggle')"><PanelLeftOpen v-if="collapsed" :size="19" /><PanelLeftClose v-else :size="19" /></button>
    <div class="brand"><div class="brand-mark">DA</div><strong>数据分析智能体</strong></div>
    <button class="new-task" type="button" title="新建分析" aria-label="新建分析" @click="newTask"><Plus :size="17" /><span>新建分析</span></button>
    <p class="side-label">工作区</p>
    <router-link class="nav-item" :to="currentAnalysisPath" title="当前分析" aria-label="当前分析"><BarChart3 :size="17" /><span>当前分析</span></router-link>
    <router-link class="nav-item" to="/history" title="历史任务" aria-label="历史任务"><Clock3 :size="17" /><span>历史任务</span></router-link>
    <p class="side-label">本地数据</p>
    <router-link class="nav-item" to="/datasets" title="数据集" aria-label="数据集"><Database :size="17" /><span>数据集</span></router-link>
    <router-link class="nav-item" to="/reports" title="报告库" aria-label="报告库"><FileText :size="17" /><span>报告库</span></router-link>
    <div class="recent-file"><span class="file-badge"><FileSpreadsheet :size="15" /></span><span><strong>数据仅保存在本机</strong><small>支持 XLSX 与 CSV</small></span></div>
    <div class="account"><span class="avatar"><UserRound :size="15" /></span><span><strong>本地工作区</strong><small>Ollama · qwen3.5:4b</small></span></div>
  </aside>
</template>
