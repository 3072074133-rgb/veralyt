<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { Check, GitBranch, RotateCcw, X } from 'lucide-vue-next'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import { api } from '../api'
import type { NodeExecutionDetail, NodeExecutionSummary, WorkflowRun } from '../types'

const props = defineProps<{ open: boolean; taskId?: string; activeRunId?: string; running: boolean }>()
const emit = defineEmits<{ close: []; replayed: []; activated: [] }>()
const runs = ref<WorkflowRun[]>([])
const nodes = ref<NodeExecutionSummary[]>([])
const selectedRunId = ref('')
const selectedNode = ref<NodeExecutionDetail>()
const promptContent = ref('')
const loading = ref(false)
const submitting = ref(false)
const drawer = ref<HTMLElement>()
let previousFocus: HTMLElement | null = null

const selectedRun = computed(() => runs.value.find((item) => item.id === selectedRunId.value))
const canActivateSelected = computed(() => !!selectedRun.value && ['completed', 'completed_with_warnings'].includes(selectedRun.value.status))
const changed = computed(() => !!selectedNode.value && promptContent.value.trim() !== selectedNode.value.prompt_version.content.trim())
const canReplay = computed(() => selectedNode.value?.prompt_replay_supported && changed.value && promptContent.value.trim().length >= 20 && !props.running && !submitting.value)
const downstream = computed(() => {
  const names: Record<string, string[]> = {
    classify: ['意图分类', '分析计划', '工具调度', '草稿生成', '校验', '反思复核'],
    plan: ['分析计划', '工具调度', '草稿生成', '校验', '反思复核'],
    execute: ['工具调度', '草稿生成', '校验', '反思复核'],
    draft: ['草稿生成', '校验', '反思复核'],
    reflect: ['反思复核及其返工路线'],
  }
  return selectedNode.value ? names[selectedNode.value.node_name].join('、') : ''
})

watch(() => props.open, async (open) => {
  if (open) { previousFocus = document.activeElement as HTMLElement; await loadRuns(); await nextTick(); drawer.value?.focus() }
  else previousFocus?.focus()
})
function onKeydown(event: KeyboardEvent) { if (event.key === 'Escape') emit('close') }

async function loadRuns() {
  if (!props.taskId) return
  loading.value = true
  try {
    runs.value = await api.listRuns(props.taskId)
    const completed = new Set(['completed', 'completed_with_warnings'])
    const preferred = runs.value.find((item) => item.id === props.activeRunId && completed.has(item.status))
      ?? runs.value.find((item) => completed.has(item.status))
      ?? runs.value[0]
    if (preferred) await selectRun(preferred.id)
  } catch (reason) {
    ElMessage.error(reason instanceof Error ? reason.message : '节点记录加载失败')
  } finally { loading.value = false }
}

async function selectRun(runId: string) {
  if (!props.taskId) return
  selectedRunId.value = runId
  selectedNode.value = undefined
  promptContent.value = ''
  nodes.value = await api.listRunNodes(props.taskId, runId)
}

async function selectNode(node: NodeExecutionSummary) {
  if (!props.taskId || node.status === 'running') return
  selectedNode.value = await api.getNodeExecution(props.taskId, node.id)
  promptContent.value = selectedNode.value.prompt_version.content
}

async function replay() {
  if (!props.taskId || !selectedNode.value || !canReplay.value) return
  submitting.value = true
  try {
    await api.replayNode(props.taskId, selectedNode.value.id, promptContent.value.trim())
    ElMessage.success('新提示词版本已保存，重跑分支已进入队列')
    emit('replayed')
    emit('close')
  } catch (reason) {
    ElMessage.error(reason instanceof Error ? reason.message : '重跑创建失败')
  } finally { submitting.value = false }
}

async function activate(run: WorkflowRun) {
  if (!props.taskId || run.is_active || !run.result || props.running) return
  try {
    await api.activateRun(props.taskId, run.id)
    await loadRuns()
    emit('activated')
    ElMessage.success('已切换到所选分析分支')
  } catch (reason) {
    ElMessage.error(reason instanceof Error ? reason.message : '分支切换失败')
  }
}

function nodeLabel(node: NodeExecutionSummary) {
  const labels = { classify: '意图分类', plan: '分析计划', execute: '工具调度', draft: '草稿生成', reflect: '反思复核' }
  return `${labels[node.node_name]}${node.occurrence > 1 ? ` · 第 ${node.occurrence} 次` : ''}`
}

function runLabel(run: WorkflowRun, index: number) {
  if (!run.parent_run_id) return '原始分析'
  const total = runs.value.filter((item) => item.parent_run_id).length
  const newer = runs.value.slice(0, index).filter((item) => item.parent_run_id).length
  return `重跑分支 ${total - newer}`
}
function runStatus(status: string) { return ({ queued: '排队中', running: '运行中', completed: '已完成', completed_with_warnings: '完成但有警告', needs_review: '待人工复核', cancelled: '已取消', failed: '失败', off_topic: '非分析问题', needs_clarification: '需要澄清' } as Record<string, string>)[status] ?? status }
function shortId(value: string) { return value.slice(0, 8) }
function formatTime(value: string) { return new Date(value).toLocaleString('zh-CN', { hour12: false }) }
function pretty(value: unknown) { return JSON.stringify(value, null, 2) }
</script>

<template>
  <div v-if="open" class="node-drawer-backdrop" @click.self="emit('close')">
    <aside ref="drawer" class="node-replay-drawer" role="dialog" aria-modal="true" aria-labelledby="node-drawer-title" tabindex="-1" @keydown="onKeydown">
      <header>
        <div><h2 id="node-drawer-title">节点记录</h2></div>
        <button class="icon-button" title="关闭" @click="emit('close')"><X :size="20" /></button>
      </header>
      <div class="node-drawer-body" :class="{ loading }">
        <nav class="run-navigation">
          <strong>分析分支</strong>
          <button v-for="(run, index) in runs" :key="run.id" :class="['run-row', {selected: run.id === selectedRunId}]" @click="selectRun(run.id)">
            <span><GitBranch :size="15" /><b>{{ runLabel(run, index) }}</b></span>
            <small>{{ shortId(run.id) }} · {{ runStatus(run.status) }}</small>
            <i v-if="run.is_active"><Check :size="12" />当前</i>
          </button>
          <div v-if="selectedRun?.result" class="run-result-preview">
            <strong>{{ selectedRun.result.title }}</strong><p>{{ selectedRun.result.summary }}</p>
            <button v-if="!selectedRun.is_active && canActivateSelected" class="button" :disabled="running" @click="activate(selectedRun)">设为当前结果</button>
          </div>
          <strong class="node-heading">节点记录</strong>
          <button v-for="node in nodes" :key="node.id" :class="['node-row', {selected: node.id === selectedNode?.id}]" :disabled="node.status === 'running'" @click="selectNode(node)">
            <span>{{ nodeLabel(node) }}</span><small>提示词 {{ node.prompt_version.version }} · {{ node.status === 'failed' ? '失败' : node.status === 'running' ? '运行中' : '完成' }}</small>
          </button>
          <p v-if="!nodes.length && !loading" class="legacy-note">该分支创建于节点快照功能启用前，不能可靠回溯。</p>
        </nav>

        <section v-if="selectedNode" class="node-editor">
          <header><div><h3>{{ nodeLabel(selectedNode) }}</h3><p>{{ formatTime(selectedNode.started_at) }} · 状态模型 v{{ selectedNode.state_schema_version }}</p></div><code>{{ selectedNode.prompt_version.content_hash.slice(0, 12) }}</code></header>
          <label><span>节点提示词</span><textarea v-model="promptContent" :readonly="!selectedNode.prompt_replay_supported" spellcheck="false" /></label>
          <p v-if="selectedNode.replay_unavailable_reason">{{ selectedNode.replay_unavailable_reason }}</p>
          <div class="replay-impact"><strong>重新执行范围</strong><span>{{ downstream }}</span></div>
          <div class="editor-actions"><span v-if="!changed">提示词未修改</span><button class="button primary" :disabled="!canReplay" @click="replay"><RotateCcw :size="16" />保存新版本并重跑</button></div>
          <details><summary>节点输入快照</summary><pre>{{ pretty(selectedNode.input_state) }}</pre></details>
          <details><summary>节点输出快照</summary><pre>{{ pretty(selectedNode.output_state) }}</pre></details>
          <details><summary>模型调用诊断</summary><pre>{{ pretty(selectedNode.diagnostics) }}</pre></details>
        </section>
        <section v-else class="node-editor-empty"><GitBranch :size="28" /><p>未选择节点</p></section>
      </div>
    </aside>
  </div>
</template>
