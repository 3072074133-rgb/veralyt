<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { BookOpen, Database, Download, FileSpreadsheet, Link2, Paperclip, RefreshCw, Send, X } from 'lucide-vue-next'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import AnalysisChart from '../components/AnalysisChart.vue'
import { api } from '../api'
import { buildConversationTurns, resolveRetryQuestion, type ConversationTurn } from '../conversation'
import { useTaskStore } from '../stores/task'

const EvidenceDrawer = defineAsyncComponent(() => import('../components/EvidenceDrawer.vue'))
const DatasetWorkspace = defineAsyncComponent(() => import('../components/DatasetWorkspace.vue'))
const DataSourceDrawer = defineAsyncComponent(() => import('../components/DataSourceDrawer.vue'))
const KnowledgeBindingDrawer = defineAsyncComponent(() => import('../components/KnowledgeBindingDrawer.vue'))

const route = useRoute()
const router = useRouter()
const store = useTaskStore()
const prompt = ref('')
const input = ref<HTMLInputElement>()
const drawerOpen = ref(false)
const sourceDrawerOpen = ref(false)
const knowledgeDrawerOpen = ref(false)
const datasetWorkspaceOpen = ref(false)
const workspaceInitialDatasetId = ref('')
const chartEvidence = ref<Record<string, Awaited<ReturnType<typeof import('../api').api.getEvidence>>>>({})
const chartLoading = ref<Record<string, boolean>>({})
const chartErrors = ref<Record<string, string>>({})
const relationSaving = ref(false)

const terminal = new Set(['ready', 'off_topic', 'needs_clarification', 'needs_review', 'completed_with_warnings', 'completed', 'failed', 'cancelled'])
const canSend = computed(() => !!prompt.value.trim() && !!store.task?.files.some((file) => file.status === 'ready') && !store.isRunning)
const sourceCount = computed(() => (store.task?.files.length ?? 0) + store.uploadFailures.length)
const conversationTurns = computed(() => buildConversationTurns(
  store.task?.messages ?? [],
  store.runs,
  store.task?.pending_run_id,
  store.task?.active_run_id,
))
onMounted(() => initialize())
watch(() => route.params.id, () => initialize())
onBeforeUnmount(() => store.closeEvents())

async function initialize() {
  const id = route.params.id as string | undefined
  sourceDrawerOpen.value = false
  knowledgeDrawerOpen.value = false
  chartEvidence.value = {}
  chartErrors.value = {}
  if (id) {
    try { await store.loadTask(id) }
    catch (reason) {
      ElMessage.error(reason instanceof Error ? reason.message : '任务加载失败')
      if (id === store.currentTaskId) {
        store.startNewAnalysis()
        await router.replace('/')
        return
      }
    }
  } else if (store.currentTaskId) {
    await router.replace(`/tasks/${store.currentTaskId}`)
    return
  } else store.startNewAnalysis()
  await loadChartEvidence()
}

async function confirmRelationships() {
  if (!store.task) return
  const candidates = store.task.relationships.filter((item) => item.status === 'candidate')
  if (!candidates.length) return
  relationSaving.value = true
  try {
    await api.saveTaskRelationships(store.task.id, candidates.map((item) => ({ ...item, status: 'confirmed' as const })))
    await store.loadTask(store.task.id)
    ElMessage.success('已确认表间关系，后续查询可安全关联')
  } catch (reason) {
    ElMessage.error(reason instanceof Error ? reason.message : '关系确认失败')
  } finally { relationSaving.value = false }
}

watch(() => store.task?.result, loadChartEvidence, { deep: true })
async function loadChartEvidence() {
  for (const chart of store.task?.result?.charts ?? []) {
    if (!chartEvidence.value[chart.dataset_ref]) {
      chartLoading.value[chart.dataset_ref] = true
      try {
        chartEvidence.value[chart.dataset_ref] = await import('../api').then(({ api }) => api.getEvidence(store.task!.id, chart.dataset_ref))
        delete chartErrors.value[chart.dataset_ref]
      } catch (reason) {
        chartErrors.value[chart.dataset_ref] = reason instanceof Error ? reason.message : '图表数据加载失败'
      } finally { chartLoading.value[chart.dataset_ref] = false }
    }
  }
}

async function pickFiles(event: Event) {
  const files = Array.from((event.target as HTMLInputElement).files ?? [])
  if (files.length) {
    if (!store.task) {
      try {
        const created = await store.createTask()
        await router.replace(`/tasks/${created}`)
      } catch (reason) {
        ElMessage.error(reason instanceof Error ? reason.message : '任务创建失败')
        return
      }
    }
    const result = await store.upload(files)
    if (!result) return
    if (result.outcome === 'failed') { sourceDrawerOpen.value = true; ElMessage.error(store.error || '文件解析失败') }
    else if (result.outcome === 'partial') { sourceDrawerOpen.value = true; ElMessage.warning(`已读取 ${result.accepted_files.length} 个文件，${result.rejected_files.length} 个失败`) }
    else ElMessage.success(`已读取 ${result.accepted_files.length} 个文件`)
  }
  if (input.value) input.value.value = ''
}

async function submit(text = prompt.value) {
  const content = text.trim()
  if (!content || !store.task) return
  prompt.value = ''
  try { await store.send(content) }
  catch { ElMessage.error(store.error || '发送失败') }
}

function formatTime(value?: string) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '' }
function isActiveTurn(turn: ConversationTurn) {
  return store.runs.some((run) => (
    run.message_sequence === turn.user.sequence && run.id === store.task?.active_run_id
  ))
}
function isPendingTurn(turn: ConversationTurn) { return turn.run?.id === store.task?.pending_run_id }
function download(kind: 'excel' | 'report') { if (store.task) window.location.href = `/api/v1/tasks/${store.task.id}/exports/${kind}` }
async function openEvidence(id?: string) {
  if (!id) return
  try { await store.openEvidence(id); drawerOpen.value = true }
  catch (reason) { ElMessage.error(reason instanceof Error ? reason.message : '证据加载失败') }
}
async function cancelQueuedRun() {
  try { await store.cancelQueuedRun(); ElMessage.success('已取消排队中的分析') }
  catch { ElMessage.error(store.error || '取消失败') }
}
async function stopCurrentRun() {
  try { await store.stopCurrentRun(); ElMessage.success('已中止当前分析') }
  catch { ElMessage.error(store.error || '中止失败') }
}
async function publishReport() {
  if (!store.task) return
  try {
    const job = await api.publishReport(store.task.id)
    ElMessage.success('报告已进入生成队列')
    void waitForReport(job.id)
  }
  catch (reason) { ElMessage.error(reason instanceof Error ? reason.message : '报告发布失败') }
}
async function waitForReport(jobId: string) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 1000))
    const job = await api.getReportJob(jobId)
    if (job.status === 'ready') { ElMessage.success('正式报告已发布到报告库'); return }
    if (job.status === 'failed') { ElMessage.error(job.error || '报告生成失败'); return }
  }
}
async function refreshAfterDatasetChange() {
  if (store.task) await store.loadTask(store.task.id)
  chartEvidence.value = {}
}
async function refreshAfterKnowledgeChange() {
  if (store.task) await store.loadTask(store.task.id)
}
function openDatasetWorkspace(datasetId?: string) {
  sourceDrawerOpen.value = false
  workspaceInitialDatasetId.value = datasetId ?? ''
  datasetWorkspaceOpen.value = true
}
</script>

<template>
  <main class="workspace">
    <header class="topbar">
      <div><h1>{{ store.task?.title ?? '新建分析' }}</h1><p>{{ store.task?.status_message ?? '准备数据后开始分析' }}</p></div>
      <div class="top-actions" v-if="store.task && sourceCount">
        <button class="button source-trigger" @click="sourceDrawerOpen=true"><Database :size="16" />数据来源 <span>{{ sourceCount }}</span></button>
        <button class="button source-trigger" @click="knowledgeDrawerOpen=true"><BookOpen :size="16" />知识库 <span>{{ store.task.knowledge_bases.length }}</span></button>
        <button v-if="terminal.has(store.task.status) && store.task.result" class="button" @click="publishReport"><Download :size="16" />发布报告</button>
        <button v-if="terminal.has(store.task.status) && store.task.result" class="button primary" @click="download('excel')"><Download :size="16" />Excel 结果</button>
      </div>
    </header>

    <section class="content">
      <div v-if="!store.task?.files.length && !store.isRunning" class="empty-state">
        <div class="empty-symbol"><FileSpreadsheet :size="28" /></div>
        <h2>把表格交给我，直接说你想分析什么</h2>
        <p>支持 Excel 和 CSV。数据只保存在这台电脑，可分析汇总、趋势、同比环比、贡献度和异常。</p>
        <div class="starter-grid">
          <button @click="prompt='按月份分析收入和利润变化，并找出异常月份'"><strong>月度经营分析</strong><span>分析收入、成本和利润趋势</span></button>
          <button @click="prompt='按部门比较预算与实际费用，找出超预算项目'"><strong>预算执行检查</strong><span>定位部门与科目差异</span></button>
          <button @click="prompt='分析各区域销售额和回款率，按表现排序'"><strong>销售与回款</strong><span>比较区域表现和回款情况</span></button>
          <button @click="prompt='检查数据中的异常金额、重复记录和缺失字段'"><strong>数据质量检查</strong><span>识别异常、重复和缺失</span></button>
        </div>
      </div>

      <section v-if="store.uploadFailures.length" class="upload-failures" aria-label="未导入文件">
        <strong>以下文件未导入</strong>
        <ul><li v-for="item in store.uploadFailures" :key="item.name"><span>{{ item.name }}</span><small>{{ item.message }}</small></li></ul>
      </section>

      <section v-if="store.task?.relationships?.length" class="relationship-panel result-panel">
        <header><span><strong><Link2 :size="15" />表间关系候选</strong><small>{{ store.task.relationships.length }} 条</small></span><button v-if="store.task.relationships.some((item) => item.status === 'candidate')" class="button primary" :disabled="relationSaving || store.isRunning" @click="confirmRelationships">确认候选关系</button></header>
        <div class="relationship-list"><article v-for="relation in store.task.relationships" :key="`${relation.left_dataset_id}-${relation.left_field}-${relation.right_dataset_id}-${relation.right_field}`"><span>{{ relation.left_dataset }} · {{ relation.left_field }}</span><Link2 :size="14" /><span>{{ relation.right_dataset }} · {{ relation.right_field }}</span><em :class="relation.status">{{ relation.status === 'confirmed' ? '已确认' : relation.status === 'rejected' ? '已排除' : `候选 ${Math.round(relation.confidence * 100)}%` }}</em></article></div>
      </section>

      <section v-for="turn in conversationTurns" :key="turn.user.id" class="conversation-turn">
        <div class="user-message">{{ turn.user.content }}</div>

        <section v-if="store.isRunning && isPendingTurn(turn)" class="progress-panel compact-progress">
          <div class="progress-head"><div><h2>{{ store.task?.queue_position && store.task.queue_position > 0 ? '正在排队' : '正在思考' }}</h2><p v-if="store.task?.queue_position && store.task.queue_position > 0">当前队列位置：{{ store.task.queue_position }}</p><p v-else>可以随时中止本次请求。</p></div><div><button v-if="store.task?.queue_position && store.task.queue_position > 0" class="button" @click="cancelQueuedRun"><X :size="15" />取消排队</button><button v-else-if="store.task?.pending_run_id && store.task.queue_position === 0" class="button danger" @click="stopCurrentRun"><X :size="15" />中止分析</button></div></div>
        </section>

        <div v-if="!isActiveTurn(turn) && !(turn.run?.status === 'needs_clarification' && store.task?.status === 'needs_clarification')" class="assistant-messages">
          <div v-for="message in turn.assistantMessages" :key="message.id" class="assistant-message">{{ message.content }}</div>
        </div>

        <section v-if="turn.run?.status === 'needs_clarification' && store.task?.status === 'needs_clarification'" class="clarify-panel">
          <h2>需要确认分析口径</h2><p>{{ store.task.clarification_question }}</p>
          <div class="clarify-answer"><input v-model="prompt" placeholder="输入你的选择或补充说明"><button class="button primary" :disabled="!prompt.trim()" @click="submit()">确认并继续</button></div>
        </section>

        <section v-if="turn.run?.status === 'failed'" class="notice-panel danger">
          <h2>本次请求没有完成</h2><p>{{ /IntentDecision|validation errors|JSON Schema/.test(turn.run.error || '') ? '本次问题未能可靠识别，请明确要查询的指标。详细错误请重试或缩小分析范围。' : turn.run.error || store.error || '本轮请求执行失败' }}</p>
          <button class="button primary" :disabled="store.isRunning" @click="submit(resolveRetryQuestion(turn, conversationTurns))"><RefreshCw :size="16" />重新分析</button>
        </section>

        <section v-if="turn.run?.status === 'needs_review'" class="notice-panel review">
          <h2>结果需要人工复核</h2><p>{{ turn.run.error }}</p>
        </section>

        <div v-if="isActiveTurn(turn) && store.task?.result" class="result-view">
        <header class="result-title"><div><h2>{{ store.task.result.title }}</h2><p>{{ store.task.result.summary }}</p></div><button class="button" @click="prompt='继续分析：'">继续追问</button></header>
        <section v-if="store.task.result.insights?.length" class="result-panel insights-panel"><header><strong>关键结论</strong><span>{{ store.task.result.insights.length }} 条</span></header><div class="insight-list"><article v-for="insight in store.task.result.insights" :key="insight.id" :class="['insight-card', insight.severity]"><div class="insight-head"><strong>{{ insight.title }}</strong><em>{{ insight.severity === 'error' ? '需处理' : insight.severity === 'warning' ? '关注' : '结论' }}</em></div><p>{{ insight.conclusion }}</p><small>{{ insight.significance }}</small><div v-if="insight.action" class="insight-action"><b>建议</b>{{ insight.action }}</div><button v-if="insight.evidence_refs[0]" class="text-button" @click="openEvidence(insight.evidence_refs[0])">查看证据{{ insight.formula ? ` · ${insight.formula}` : '' }}</button></article></div></section>
        <section v-if="store.task.result.metrics.length" class="metrics-row">
          <button v-for="metric in store.task.result.metrics" :key="metric.label" @click="openEvidence(metric.evidence_refs[0])"><small>{{ metric.label }}</small><strong>{{ metric.value }}</strong><span :class="metric.direction">{{ metric.change }}</span></button>
        </section>
        <div class="result-grid">
          <section v-for="chart in store.task.result.charts" :key="chart.id" class="result-panel chart-panel"><header><strong>{{ chart.title }}</strong><span>{{ chart.unit }}</span></header><AnalysisChart :spec="chart" :evidence="chartEvidence[chart.dataset_ref]" :loading="chartLoading[chart.dataset_ref]" :error="chartErrors[chart.dataset_ref]" /></section>
          <details class="result-panel findings-panel"><summary><strong>原始发现</strong><span>{{ store.task.result.findings.length }} 条</span></summary><ol><li v-for="finding in store.task.result.findings" :key="finding.title"><button @click="openEvidence(finding.evidence_refs[0])"><strong>{{ finding.title }}</strong><span>{{ finding.detail }}</span></button></li></ol></details>
        </div>
        <section class="result-panel method-panel"><header><strong>口径、来源与风险提示</strong><span>结果已完成自动复核</span></header><div><article><strong>数据与知识来源</strong><p>{{ store.task.files.map((file) => file.original_name).join('、') }}，共 {{ store.task.files.reduce((sum, file) => sum + file.row_count, 0).toLocaleString() }} 行。<template v-if="store.task.knowledge_bases.length">知识库：{{ store.task.knowledge_bases.map((item) => `${item.knowledge_base_name} v${item.revision_number}`).join('、') }}。</template></p></article><article><strong>关键假设</strong><p>{{ store.task.result.assumptions.join('；') || '未使用额外假设。' }}</p></article><article><strong>风险提示</strong><p>{{ store.task.result.warnings.join('；') || '未发现需要单独提示的风险。' }}</p></article><article><strong>完成时间</strong><p>{{ formatTime(turn.run?.finished_at ?? store.task.updated_at) }}</p></article></div></section>
        <details v-if="store.task.result.calculation_details?.length" class="result-panel calculation-panel">
          <summary><span><strong>计算明细</strong><small>查看工具、结果行数与证据来源</small></span><span>{{ store.task.result.calculation_details?.length ?? 0 }} 步</span></summary>
          <div class="calculation-list">
            <article v-for="item in store.task.result.calculation_details ?? []" :key="item.id">
              <div><strong>{{ item.title }}</strong><small>{{ item.tool_name }} · {{ item.row_count }} 行</small></div>
              <span :class="['calculation-status', item.status]">{{ item.status === 'success' ? '已完成' : '失败' }}</span>
              <button v-if="item.evidence_refs[0]" class="button" @click="openEvidence(item.evidence_refs[0])">查看证据</button>
              <details v-if="item.query" class="query-detail"><summary>查看查询语句</summary><code>{{ item.query }}</code></details>
            </article>
          </div>
        </details>
        </div>
      </section>

      <section v-if="store.error && !store.uploadFailures.length && !conversationTurns.some((turn) => turn.run?.status === 'failed')" class="notice-panel danger">
        <h2>操作没有完成</h2><p>{{ store.error }}</p>
      </section>
    </section>

    <section class="composer">
      <div class="composer-box">
        <div v-if="store.task?.files.length" class="file-chips"><button v-for="file in store.task.files.slice(0, 2)" :key="file.id" :title="`查看数据来源：${file.original_name}`" @click="sourceDrawerOpen=true">{{ file.original_name }}</button><button v-if="store.task.files.length > 2" title="查看全部数据来源" @click="sourceDrawerOpen=true">+{{ store.task.files.length - 2 }}</button></div>
        <textarea v-model="prompt" rows="2" placeholder="上传数据后，直接输入你的分析需求" @keydown.enter.exact.prevent="canSend && submit()" />
        <div class="composer-actions"><div><button class="icon-button" title="上传 Excel 或 CSV" :disabled="store.busy || store.isRunning" @click="input?.click()"><Paperclip :size="19" /></button><span>最多 5 个文件，每个不超过 100 MB</span></div><button class="send-button" title="开始分析" :disabled="!canSend" @click="submit()"><Send :size="18" /></button></div>
      </div>
      <input ref="input" hidden type="file" multiple accept=".xlsx,.csv" @change="pickFiles">
    </section>
    <EvidenceDrawer :open="drawerOpen" :evidence="store.evidence" @close="drawerOpen=false" />
    <DataSourceDrawer :open="sourceDrawerOpen" :files="store.task?.files ?? []" :datasets="store.task?.datasets ?? []" :upload-failures="store.uploadFailures" :running="store.isRunning" @close="sourceDrawerOpen=false" @add-files="input?.click()" @open-workspace="openDatasetWorkspace" />
    <KnowledgeBindingDrawer :open="knowledgeDrawerOpen" :task-id="store.task?.id" :bindings="store.task?.knowledge_bases ?? []" :running="store.isRunning" @close="knowledgeDrawerOpen=false" @updated="refreshAfterKnowledgeChange" />
    <DatasetWorkspace :open="datasetWorkspaceOpen" :task-id="store.task?.id" :datasets="store.task?.datasets ?? []" :initial-dataset-id="workspaceInitialDatasetId" :running="store.isRunning" @close="datasetWorkspaceOpen=false" @published="refreshAfterDatasetChange" />
  </main>
</template>
