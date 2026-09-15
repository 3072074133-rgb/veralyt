<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ArrowLeft, Download, FileText } from 'lucide-vue-next'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import type { ReportDetail } from '../types'

const route = useRoute(); const router = useRouter(); const report = ref<ReportDetail>(); const error = ref('')
const version = computed(() => report.value?.versions.find(item => item.id === route.params.versionId) ?? report.value?.versions[0])
onMounted(async () => { try { report.value = await api.getReport(route.params.id as string) } catch (reason) { error.value = reason instanceof Error ? reason.message : '报告加载失败' } })
function versionLabel(item: ReportDetail['versions'][number]) { return item.content.analysis.report_schema_version === 2 ? `报告结构 v2 · 发布版本 ${item.version_number}` : `报告版本 v${item.version_number}` }
</script>
<template>
  <main class="workspace published-report">
    <header class="topbar"><div><h1><FileText :size="18" />{{ report?.title ?? '已发布报告' }}</h1><p v-if="version">{{ versionLabel(version) }} · 独立发布快照</p></div><div class="top-actions"><button class="button" @click="router.push('/reports')"><ArrowLeft :size="16" />返回报告库</button><a v-if="version" class="button primary" :href="`/api/v1/reports/${report!.id}/versions/${version.id}/download`"><Download :size="16" />下载报告</a></div></header>
    <section v-if="version" class="published-frame"><iframe :src="`/api/v1/reports/${report!.id}/versions/${version.id}/preview`" title="已发布报告" /></section>
    <section v-else class="empty-state"><p>{{ error || '正在加载报告…' }}</p></section>
  </main>
</template>
<style scoped>
.published-report{min-height:100vh}.published-report .topbar h1{display:flex;align-items:center;gap:8px}.published-frame{padding:24px;flex:1;min-height:calc(100vh - 58px);background:#f4f6f6}.published-frame iframe{display:block;width:100%;height:calc(100vh - 106px);min-height:720px;border:1px solid #dce5e8;background:#fff;border-radius:6px}.top-actions{display:flex;gap:8px;align-items:center}@media(max-width:700px){.published-frame{padding:10px}.top-actions .button{padding:0 8px}.published-frame iframe{height:calc(100vh - 90px)}}
</style>
