<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { BarChart, LineChart, PieChart, ScatterChart } from 'echarts/charts'
import { DataZoomComponent, GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart3, Download, RotateCcw, Table2 } from 'lucide-vue-next'
import { buildChartOption, prepareChartRows } from '../chart-options'
import type { ChartSpec, EvidenceRecord } from '../types'

echarts.use([BarChart, LineChart, PieChart, ScatterChart, DataZoomComponent, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

const props = defineProps<{ spec: ChartSpec; evidence?: EvidenceRecord; loading?: boolean; error?: string }>()
const target = ref<HTMLDivElement>()
const view = ref<'chart' | 'table'>(props.spec.chart_type === 'table' ? 'table' : 'chart')
const compact = ref(false)
let chart: echarts.ECharts | undefined
let observer: ResizeObserver | undefined
const rows = computed(() => prepareChartRows(props.spec, props.evidence))
const columns = computed(() => props.spec.chart_type === 'scatter'
  ? [props.spec.label_field, props.spec.x_field, props.spec.y_field].filter((value): value is string => !!value)
  : [props.spec.category_field, ...props.spec.series.map((item) => item.field)])
const option = computed(() => buildChartOption(props.spec, props.evidence, compact.value))
const zoomable = computed(() => !['pie', 'scatter', 'table'].includes(props.spec.chart_type) && rows.value.length > 12)

function render() { if (chart) chart.setOption(option.value, true) }
async function ensureChart() {
  await nextTick()
  if (!target.value || view.value !== 'chart' || props.spec.chart_type === 'table') return
  if (chart && chart.getDom() !== target.value) { chart.dispose(); chart = undefined }
  if (!chart) chart = echarts.init(target.value)
  if (!observer && typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver((entries) => {
      compact.value = entries[0]?.contentRect.width < 520
      resize()
    })
    observer.observe(target.value)
  }
  render()
}
onMounted(ensureChart)
watch(option, render, { deep: true })
watch(() => [props.loading, props.error, props.evidence], ensureChart, { deep: true })
watch(() => props.spec.id, () => { view.value = props.spec.chart_type === 'table' ? 'table' : 'chart' })
watch(view, (value) => { if (value === 'chart') void ensureChart() })
function resize() { chart?.resize() }
function resetZoom() {
  const end = Math.max(25, Math.round(12 / rows.value.length * 100))
  chart?.dispatchAction({ type: 'dataZoom', start: 0, end })
}
function downloadCurrentView() {
  if (view.value === 'chart' && chart) {
    triggerDownload(chart.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#ffffff' }), `${safeName(props.spec.title)}.png`)
    return
  }
  const csv = `\ufeff${columns.value.map(csvCell).join(',')}\r\n${rows.value.map((row) => columns.value.map((column) => csvCell(row[column])).join(',')).join('\r\n')}`
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }))
  triggerDownload(url, `${safeName(props.spec.title)}.csv`)
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}
function triggerDownload(url: string, name: string) {
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  anchor.click()
}
function safeName(value: string) { return value.replace(/[\\/:*?"<>|]/g, '_') || '分析图表' }
function csvCell(value: unknown) {
  const raw = String(value ?? '')
  const safe = typeof value !== 'number' && Number.isNaN(Number(raw.replaceAll(',', ''))) && /^[=+\-@\t\r]/.test(raw) ? `'${raw}` : raw
  return `"${safe.replaceAll('"', '""')}"`
}
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose() })
</script>

<template>
  <div v-if="loading" class="chart-state" role="status">正在加载图表数据</div>
  <div v-else-if="error" class="chart-state error" role="alert">{{ error }}</div>
  <div v-else-if="!evidence?.rows.length" class="chart-state">没有可展示的数据</div>
  <div v-else class="chart-workspace">
    <div class="chart-toolbar">
      <div class="chart-view-switch" role="group" aria-label="图表视图">
        <button title="查看图表" aria-label="查看图表" :class="{active: view === 'chart'}" :aria-pressed="view === 'chart'" :disabled="spec.chart_type === 'table'" @click="view='chart'"><BarChart3 :size="15" /></button>
        <button title="查看图表数据" aria-label="查看图表数据" :class="{active: view === 'table'}" :aria-pressed="view === 'table'" @click="view='table'"><Table2 :size="15" /></button>
      </div>
      <span>{{ rows.length }} / {{ evidence.rows.length }} 行</span>
      <button v-if="view === 'chart' && zoomable" class="icon-button" title="重置图表缩放" aria-label="重置图表缩放" @click="resetZoom"><RotateCcw :size="15" /></button>
      <button class="icon-button" :title="view === 'chart' ? '下载 PNG' : '下载 CSV'" :aria-label="view === 'chart' ? '下载 PNG' : '下载 CSV'" @click="downloadCurrentView"><Download :size="15" /></button>
    </div>
    <div v-show="view === 'chart'" ref="target" class="analysis-chart" role="img" :aria-label="spec.title" />
    <div v-show="view === 'table'" class="chart-table-view">
      <table><thead><tr><th v-for="column in columns" :key="column">{{ column }}</th></tr></thead><tbody><tr v-for="(row, index) in rows" :key="index"><td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td></tr></tbody></table>
    </div>
  </div>
</template>
