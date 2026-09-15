<script setup lang="ts">
import { computed } from 'vue'
import { ExternalLink } from 'lucide-vue-next'
import AnalysisChart from './AnalysisChart.vue'
import { reportSections, type ChapterDraft } from '../report-document'
import type { EvidenceRecord } from '../types'

const props = defineProps<{ report: ChapterDraft; evidence: Record<string, EvidenceRecord>; loading: Record<string, boolean>; errors: Record<string, string> }>()
defineEmits<{ evidence: [id: string] }>()
const sections = computed(() => reportSections(props.report))
const anchor = (index: number) => `report-section-${index}`
</script>

<template>
  <div class="chapter-document">
    <nav v-if="sections.length > 3" aria-label="报告章节"><a v-for="(section, index) in sections" :key="section.id" :href="`#${anchor(index)}`">{{ section.title }}</a></nav>
    <section v-for="(section, index) in sections" :id="anchor(index)" :key="section.id" class="report-chapter">
      <h3>{{ section.title }}</h3>
      <div v-for="block in section.blocks" :key="block.id" class="chapter-block">
        <p v-if="block.kind === 'paragraph'">{{ block.text }}</p>
        <ul v-else-if="block.kind === 'list'"><li v-for="(item, i) in block.items" :key="i"><span>{{ item.text }}</span><button v-for="id in item.evidence_refs" :key="id" class="citation" title="查看证据" @click="$emit('evidence', id)"><ExternalLink :size="13" /><span>证据</span></button></li></ul>
        <div v-else-if="block.kind === 'metrics'" class="chapter-metrics"><article v-for="(metric, i) in block.metrics" :key="i"><small>{{ metric.label }}</small><strong>{{ metric.value }}</strong><span v-if="metric.change">{{ metric.change }}</span><button v-for="id in metric.evidence_refs" :key="id" class="citation" title="查看证据" @click="$emit('evidence', id)"><ExternalLink :size="13" /><span>证据</span></button></article></div>
        <template v-else-if="block.kind === 'table' && block.dataset_ref">
          <p v-if="loading[block.dataset_ref]" role="status">正在读取数据…</p>
          <p v-else-if="errors[block.dataset_ref]" role="alert">{{ errors[block.dataset_ref] }}</p>
          <div v-else-if="evidence[block.dataset_ref]" class="chapter-table"><table><thead><tr><th v-for="column in (block.columns.length ? block.columns : evidence[block.dataset_ref]!.columns)" :key="column">{{ column }}</th></tr></thead><tbody><tr v-for="(row, i) in evidence[block.dataset_ref]!.rows" :key="i"><td v-for="column in (block.columns.length ? block.columns : evidence[block.dataset_ref]!.columns)" :key="column">{{ row[column] }}</td></tr></tbody></table></div>
          <p v-else>引用的数据证据不可用。</p>
          <button class="citation" title="查看表格证据" @click="$emit('evidence', block.dataset_ref)"><ExternalLink :size="13" /><span>表格证据</span></button>
        </template>
        <template v-else-if="block.kind === 'chart' && block.chart"><h4>{{ block.chart.title }}</h4><AnalysisChart :spec="block.chart" :evidence="evidence[block.chart.dataset_ref]" :loading="loading[block.chart.dataset_ref]" :error="errors[block.chart.dataset_ref]" /><button class="citation" title="查看图表证据" @click="$emit('evidence', block.chart.dataset_ref)"><ExternalLink :size="13" /><span>图表证据</span></button></template>
        <button v-for="id in block.evidence_refs" :key="id" class="citation" title="查看证据" @click="$emit('evidence', id)"><ExternalLink :size="13" /><span>证据</span></button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.chapter-document{min-width:0;max-width:100%;color:#343738}
nav{display:flex;flex-wrap:wrap;gap:10px 20px;padding:12px 0 20px;border-bottom:1px solid #ddd}nav a{color:#42695b;text-decoration:none;font-size:13px}
.report-chapter{padding:24px 0;border-bottom:1px solid #e4e4e4;scroll-margin-top:80px;min-width:0}
h3{font-size:19px;line-height:1.5;margin:0 0 18px;overflow-wrap:anywhere}h4{font-size:15px;margin:0 0 12px}
.chapter-block{margin:0 0 18px;min-width:0;max-width:100%}.chapter-block:last-child{margin-bottom:0}
p,li>span{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.9;font-size:14px}p{margin:0}ul{padding-left:22px;margin:0}li{margin:0 0 12px}
.citation{display:inline-flex;align-items:center;gap:4px;border:0;background:transparent;color:#42695b;padding:5px 6px;font-size:11px}
.chapter-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(180px,100%),1fr));gap:20px}.chapter-metrics article{min-width:0;border-left:2px solid #ddd;padding:8px 14px}.chapter-metrics small,.chapter-metrics strong,.chapter-metrics article>span{display:block;overflow-wrap:anywhere}.chapter-metrics strong{font-size:23px;margin:8px 0}.chapter-metrics small,.chapter-metrics article>span{font-size:12px;color:#666}
.chapter-table{overflow:auto;max-height:500px;width:100%}table{border-collapse:collapse;width:100%;font-size:12px}td,th{padding:10px;border:1px solid #ddd;text-align:left;min-width:90px;max-width:350px;overflow-wrap:anywhere}th{position:sticky;top:0;background:#f2f4f3}
@media(max-width:600px){h3{font-size:17px}.report-chapter{padding:18px 0}.chapter-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.chapter-metrics strong{font-size:19px}}
</style>
