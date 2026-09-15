import type { AnalysisDraft, ReportBlock, ReportSection } from './types'
export type ChapterDraft = AnalysisDraft

export function reportSections(draft: ChapterDraft): ReportSection[] {
  if (draft.report_schema_version === 2) return draft.sections ?? []
  const sections: ReportSection[] = []
  const block = (id: string, values: Partial<ReportBlock>): ReportBlock => ({
    id, kind: 'paragraph', text: '', items: [], metrics: [], columns: [], evidence_refs: [], evidence_pointers: [], ...values,
  })
  if (draft.insights?.length) sections.push({ id: 'legacy-insights', title: '关键结论', blocks: draft.insights.map((item, i) => block(`legacy-insight-${i}`, {
    text: [item.title, item.conclusion, item.significance, item.action ? `建议：${item.action}` : ''].filter(Boolean).join('\n'),
    evidence_refs: item.evidence_refs, evidence_pointers: item.evidence_pointers,
  })) })
  if (draft.metrics.length) sections.push({ id: 'legacy-metrics', title: '关键指标', blocks: [block('legacy-metric-group', { kind: 'metrics', metrics: draft.metrics })] })
  if (draft.charts.length) sections.push({ id: 'legacy-charts', title: '主要图表', blocks: draft.charts.map((chart, i) => block(`legacy-chart-${i}`, { kind: 'chart', chart })) })
  if (draft.findings.length) sections.push({ id: 'legacy-findings', title: '关键发现', blocks: [block('legacy-finding-list', {
    kind: 'list', items: draft.findings.map(item => ({ text: `${item.title}\n${item.detail}`, evidence_refs: item.evidence_refs, evidence_pointers: item.evidence_pointers })),
  })] })
  return sections
}

export function reportEvidenceIds(draft: ChapterDraft): string[] {
  return [...new Set(reportSections(draft).flatMap(section => section.blocks.flatMap(block => [
    ...(block.dataset_ref ? [block.dataset_ref] : []), ...(block.chart ? [block.chart.dataset_ref] : []),
  ])))]
}
