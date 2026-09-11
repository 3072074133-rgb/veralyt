import { describe, expect, it } from 'vitest'
import { buildChartOption, escapeHtml, formatChartValue, prepareChartRows } from './chart-options'
import type { ChartSpec, EvidenceRecord } from './types'

function fixture(count = 16) {
  const spec: ChartSpec = {
    id: 'chart-1', title: '收入趋势', chart_type: 'line', dataset_ref: 'evidence-1',
    category_field: '月份', series: [{ name: '收入', field: '收入' }], unit: '元',
    sort_order: 'category_asc', max_items: 20,
  }
  const evidence = {
    id: 'evidence-1', title: '收入', source: 'query_data', columns: ['月份', '收入'],
    rows: Array.from({ length: count }, (_, index) => ({ 月份: `2026-${count - index}`, 收入: (index + 1) * 10_000 })),
    data_revision: 1, source_dataset_ids: [], created_at: '2026-09-11T00:00:00Z',
  } satisfies EvidenceRecord
  return { spec, evidence }
}

describe('chart options', () => {
  it('sorts categories naturally and adds zoom for dense series', () => {
    const { spec, evidence } = fixture()

    expect(prepareChartRows(spec, evidence)[0].月份).toBe('2026-1')
    expect(buildChartOption(spec, evidence).dataZoom).toBeTruthy()
  })

  it('formats units and escapes tooltip content', () => {
    expect(formatChartValue(12345.678, '元')).toBe('12,345.68元')
    expect(escapeHtml('<script>')).toBe('&lt;script&gt;')
  })

  it('does not add zoom to a short series', () => {
    const { spec, evidence } = fixture(4)
    expect(buildChartOption(spec, evidence).dataZoom).toBeUndefined()
  })
})
