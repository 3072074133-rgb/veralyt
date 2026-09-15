import type { ChartSpec, EvidenceRecord } from './types'

export type ChartRow = Record<string, unknown>

export function prepareChartRows(spec: ChartSpec, evidence?: EvidenceRecord): ChartRow[] {
  const sourceRows = [...(evidence?.rows ?? [])]
  const primaryField = spec.series[0]?.field
  if (spec.sort_order === 'category_asc') {
    sourceRows.sort((left, right) => String(left[spec.category_field] ?? '').localeCompare(String(right[spec.category_field] ?? ''), 'zh-CN', { numeric: true }))
  } else if (spec.sort_order === 'value_desc' && primaryField) {
    sourceRows.sort((left, right) => Math.abs(toNumber(right[primaryField]) ?? 0) - Math.abs(toNumber(left[primaryField]) ?? 0))
  }
  const maxItems = spec.max_items ?? 20
  if (spec.chart_type === 'scatter' && sourceRows.length > maxItems && maxItems > 1) {
    return Array.from({ length: maxItems }, (_, index) => sourceRows[Math.round(index * (sourceRows.length - 1) / (maxItems - 1))])
  }
  return sourceRows.slice(0, maxItems)
}

export function buildChartOption(spec: ChartSpec, evidence?: EvidenceRecord, compact = false): Record<string, unknown> {
  const rows = prepareChartRows(spec, evidence)
  const categories = rows.map((row) => String(row[spec.category_field] ?? ''))
  const palette = ['#3778cf', '#199b8e', '#e6a23c', '#9471c2', '#df7184', '#56a6bd', '#8aa64b', '#d58147']
  const isPie = spec.chart_type === 'pie'
  const isScatter = spec.chart_type === 'scatter'
  const isHorizontal = !isPie && spec.orientation === 'horizontal'
  const zoomable = !isPie && !isScatter && categories.length > 12
  const label = categoryLabel(categories, isHorizontal, compact)
  const unit = spec.unit ?? ''
  let series: Record<string, unknown>[]

  if (isScatter && spec.x_field && spec.y_field) {
    series = [{
      name: `${spec.x_field} / ${spec.y_field}`,
      type: 'scatter',
      symbolSize: compact ? 8 : 10,
      itemStyle: { color: palette[0], opacity: .78 },
      emphasis: { focus: 'series', itemStyle: { opacity: 1 } },
      data: rows.map((row) => ({
        value: [toNumber(row[spec.x_field!]), toNumber(row[spec.y_field!])],
        label: spec.label_field ? String(row[spec.label_field] ?? '') : '',
      })),
    }]
  } else if (spec.chart_type === 'waterfall' && spec.series[0]) {
    const values = rows.map((row) => toNumber(row[spec.series[0].field]) ?? 0)
    let running = 0
    const helpers = values.map((value) => { const base = value >= 0 ? running : running + value; running += value; return base })
    series = [
      { type: 'bar', stack: 'waterfall', silent: true, tooltip: { show: false }, itemStyle: { color: 'transparent' }, data: helpers },
      {
        name: spec.series[0].name,
        type: 'bar',
        stack: 'waterfall',
        barMaxWidth: 34,
        data: values,
        itemStyle: { color: (item: { value: number }) => item.value >= 0 ? '#199b8e' : '#df7184', borderRadius: [3, 3, 0, 0] },
        label: valueLabel(values.length, 'top', unit),
      },
    ]
  } else {
    series = spec.series.map((item, index) => {
      const values = rows.map((row) => toNumber(row[item.field]))
      if (isPie) {
        return {
          name: item.name,
          type: 'pie',
          radius: ['42%', '68%'],
          center: categories.length > 7 ? ['38%', '48%'] : ['50%', '48%'],
          minAngle: 3,
          avoidLabelOverlap: true,
          itemStyle: { borderColor: '#fff', borderWidth: 2, borderRadius: 3 },
          label: categories.length > 7 ? { show: false } : { show: true, formatter: '{b}\n{d}%', color: '#4e5f66', fontSize: 10, lineHeight: 15 },
          labelLine: { show: categories.length <= 7, length: 9, length2: 10, smooth: true },
          emphasis: { scale: true, scaleSize: 5 },
          data: rows.map((row, rowIndex) => ({ name: categories[rowIndex], value: Math.max(0, toNumber(row[item.field]) ?? 0) })),
        }
      }
      const type = spec.chart_type === 'line' ? 'line' : 'bar'
      return {
        name: item.name,
        type,
        stack: spec.chart_type === 'stacked_bar' ? 'total' : undefined,
        smooth: type === 'line',
        showSymbol: type === 'line' ? categories.length <= 24 : undefined,
        symbolSize: type === 'line' ? 6 : undefined,
        barMaxWidth: isHorizontal ? 24 : 34,
        itemStyle: {
          color: type === 'bar' && spec.series.length === 1
            ? (item: { dataIndex: number }) => palette[item.dataIndex % palette.length]
            : palette[index % palette.length],
          borderRadius: isHorizontal ? [0, 3, 3, 0] : [3, 3, 0, 0],
        },
        lineStyle: { width: 2, color: palette[index % palette.length] },
        areaStyle: type === 'line' && spec.series.length === 1 ? { color: palette[index % palette.length], opacity: .08 } : undefined,
        label: spec.series.length === 1 ? valueLabel(values.length, isHorizontal ? 'right' : 'top', unit) : { show: false },
        data: values,
      }
    })
  }

  const tooltip = isScatter ? {
    trigger: 'item',
    formatter: (params: { data: { value: Array<number | null>; label?: string } }) => {
      const labelText = params.data.label ? `<strong>${escapeHtml(params.data.label)}</strong><br>` : ''
      return `${labelText}${escapeHtml(spec.x_field ?? '')}: ${formatChartValue(params.data.value[0])}<br>${escapeHtml(spec.y_field ?? '')}: ${formatChartValue(params.data.value[1])}`
    },
  } : isPie ? {
    trigger: 'item',
    formatter: (params: { name: string; value: number; percent: number }) => `<strong>${escapeHtml(params.name)}</strong><br>${formatChartValue(params.value, unit)} · ${params.percent}%`,
  } : {
    trigger: 'axis',
    axisPointer: { type: spec.chart_type === 'line' ? 'line' : 'shadow' },
    formatter: (params: Array<{ axisValueLabel: string; seriesName: string; value: unknown }>) => {
      const visible = params.filter((item) => item.seriesName)
      const heading = escapeHtml(visible[0]?.axisValueLabel ?? '')
      return [`<strong>${heading}</strong>`, ...visible.map((item) => `${escapeHtml(item.seriesName)}: ${formatChartValue(item.value, unit)}`)].join('<br>')
    },
  }

  return {
    animationDuration: 260,
    color: palette,
    textStyle: { fontFamily: 'Microsoft YaHei UI, PingFang SC, sans-serif' },
    tooltip: {
      ...tooltip,
      confine: true,
      backgroundColor: 'rgba(255,255,255,.98)',
      borderColor: '#dce5e8',
      textStyle: { color: '#243238', fontSize: 11 },
      extraCssText: 'box-shadow:0 6px 18px rgba(31,55,63,.12);border-radius:5px;',
    },
    legend: spec.series.length > 1 || isPie ? {
      type: 'scroll',
      top: isPie ? undefined : 4,
      bottom: isPie ? 4 : undefined,
      right: isPie && categories.length > 7 ? 6 : 12,
      orient: isPie && categories.length > 7 ? 'vertical' : 'horizontal',
      textStyle: { color: '#6b7a80', fontSize: 10 },
      itemWidth: 11,
      itemHeight: 7,
    } : undefined,
    grid: isPie ? undefined : {
      left: isHorizontal ? 16 : compact ? 8 : 14,
      right: isHorizontal ? 38 : 22,
      top: spec.series.length > 1 ? 40 : 20,
      bottom: zoomable ? 55 : label.rotate ? 52 : 30,
      containLabel: true,
    },
    dataZoom: zoomable ? [
      { type: 'inside', start: 0, end: Math.max(25, Math.round(12 / categories.length * 100)) },
      { type: 'slider', start: 0, end: Math.max(25, Math.round(12 / categories.length * 100)), height: 14, bottom: 8, borderColor: '#dce5e8', fillerColor: '#dcebee', handleStyle: { color: '#28778b' } },
    ] : undefined,
    xAxis: isPie ? undefined : isScatter
      ? valueAxis(spec.x_field)
      : isHorizontal
        ? valueAxis(unit)
        : { type: 'category', data: categories, boundaryGap: spec.chart_type !== 'line', axisLine: axisLine(), axisTick: { show: false }, axisLabel: label },
    yAxis: isPie ? undefined : isScatter
      ? valueAxis(spec.y_field)
      : isHorizontal
        ? { type: 'category', data: categories, inverse: true, axisLine: axisLine(), axisTick: { show: false }, axisLabel: label }
        : valueAxis(unit),
    series,
  }
}

export function formatChartValue(value: unknown, unit = ''): string {
  const numeric = toNumber(value)
  if (numeric === null) return value === null || value === undefined || value === '' ? '—' : String(value)
  const rendered = numeric.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
  return unit && !rendered.endsWith(unit) ? `${rendered}${unit}` : rendered
}

export function escapeHtml(value: unknown): string {
  return String(value).replace(/[&<>'"]/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[character]!)
}

function toNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null
  const numeric = typeof value === 'number' ? value : Number(String(value).replaceAll(',', '').replace(/%$/, ''))
  return Number.isFinite(numeric) ? Math.round(numeric * 100) / 100 : null
}

function compactNumber(value: unknown): string {
  const numeric = toNumber(value)
  if (numeric === null) return String(value ?? '')
  const absolute = Math.abs(numeric)
  if (absolute >= 100_000_000) return `${(numeric / 100_000_000).toLocaleString('zh-CN', { maximumFractionDigits: 1 })}亿`
  if (absolute >= 10_000) return `${(numeric / 10_000).toLocaleString('zh-CN', { maximumFractionDigits: 1 })}万`
  return numeric.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

function axisLine() {
  return { lineStyle: { color: '#dce5e8' } }
}

function valueAxis(name?: string) {
  return {
    type: 'value',
    name,
    scale: true,
    nameTextStyle: { color: '#6b7a80', fontSize: 10 },
    axisLabel: { color: '#6b7a80', fontSize: 10, formatter: compactNumber },
    splitLine: { lineStyle: { color: '#e8eef0', type: 'dashed' } },
  }
}

function categoryLabel(categories: string[], horizontal: boolean, compact: boolean) {
  const longest = categories.reduce((length, category) => Math.max(length, category.length), 0)
  return {
    color: '#6b7a80',
    fontSize: 10,
    hideOverlap: true,
    width: horizontal ? (compact ? 78 : 112) : compact ? 58 : 84,
    overflow: 'truncate',
    ellipsis: '…',
    rotate: horizontal ? 0 : categories.length > 12 ? 35 : longest > 6 ? 20 : 0,
  }
}

function valueLabel(count: number, position: 'top' | 'right', unit: string) {
  return count <= 8 ? { show: true, position, color: '#4e5f66', fontSize: 9, formatter: (params: { value: unknown }) => formatChartValue(params.value, unit) } : { show: false }
}
