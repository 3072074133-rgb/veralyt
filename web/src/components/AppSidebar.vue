<script setup lang="ts">
import { BarChart3, Database, FileText, Plus, Settings, Cpu, PanelLeftClose, PanelLeftOpen } from 'lucide-vue-next'
import ConversationHistory from './ConversationHistory.vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useTaskStore } from '../stores/task'
import { api } from '../api'
import type { ModelSettings } from '../types'

const router = useRouter()
defineProps<{ collapsed: boolean }>()
defineEmits<{ toggle: [] }>()
const store = useTaskStore()
const activeModel = ref<ModelSettings | null>(null)
const currentAnalysisPath = computed(() => store.currentTaskId ? `/tasks/${store.currentTaskId}` : '/')
const modelLabel = computed(() => activeModel.value ? `${activeModel.value.provider} · ${activeModel.value.model}` : '读取模型配置')
async function loadModelSettings(event?: Event) {
  const detail = (event as CustomEvent<ModelSettings> | undefined)?.detail
  if (detail) { activeModel.value = detail; return }
  try { activeModel.value = (await api.getSettings()).llm } catch { activeModel.value = null }
}
onMounted(() => { void loadModelSettings(); window.addEventListener('model-settings-updated', loadModelSettings) })
onBeforeUnmount(() => window.removeEventListener('model-settings-updated', loadModelSettings))
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
    <p class="side-label">本地数据</p>
    <router-link class="nav-item" to="/datasets" title="数据集" aria-label="数据集"><Database :size="17" /><span>数据集</span></router-link>
    <router-link class="nav-item" to="/reports" title="报告库" aria-label="报告库"><FileText :size="17" /><span>报告库</span></router-link>
    <router-link class="nav-item" to="/settings" title="模型 API 设置" aria-label="模型 API 设置"><Settings :size="17" /><span>模型设置</span></router-link>
    <ConversationHistory v-show="!collapsed" />
    <router-link class="account" to="/settings" title="打开模型设置"><span class="avatar"><Cpu :size="15" /></span><span><strong>{{ activeModel?.mode === 'openai_compatible' ? '云端模型' : '本地模型' }}</strong><small>{{ modelLabel }}</small></span></router-link>
  </aside>
</template>
