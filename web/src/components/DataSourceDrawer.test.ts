import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'
import DataSourceDrawer from './DataSourceDrawer.vue'
import type { DatasetInfo, UploadedFile } from '../types'

const file: UploadedFile = {
  id: 'file-1',
  original_name: '2026-KZE Financial Report.xlsx',
  size: 1047552,
  status: 'ready',
  sheet_count: 18,
  detected_sheet_count: 19,
  skipped_sheet_count: 1,
  row_count: 2931,
}

const dataset: DatasetInfo = {
  id: 'dataset-1',
  table_name: 'department_profit',
  display_name: '部门盈亏',
  row_count: 7,
  columns: [],
  source_region: {
    sheet_name: 'Profit',
    region_index: 0,
    source_range: 'A1:C8',
    header_start_row: 1,
    header_end_row: 1,
    data_start_row: 2,
    data_end_row: 8,
  },
}

afterEach(() => {
  document.body.innerHTML = ''
  document.body.style.overflow = ''
})

describe('DataSourceDrawer', () => {
  it('shows file details and opens the selected dataset', async () => {
    const wrapper = mount(DataSourceDrawer, {
      attachTo: document.body,
      props: { open: true, files: [file], datasets: [dataset], uploadFailures: [] },
    })

    expect(wrapper.text()).toContain('1 个文件 · 1 个数据表 · 2,931 行')
    expect(wrapper.text()).toContain('已跳过 1 个隐藏或空白工作表')

    await wrapper.get('#source-datasets-tab').trigger('click')
    await wrapper.get('[aria-label="预览 部门盈亏"]').trigger('click')
    expect(wrapper.emitted('openWorkspace')).toEqual([['dataset-1']])

    await wrapper.get('.source-drawer').trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('surfaces rejected files and emits the add-file action', async () => {
    const wrapper = mount(DataSourceDrawer, {
      props: {
        open: true,
        files: [],
        datasets: [],
        uploadFailures: [{ name: 'broken.xlsx', code: 'invalid_file', message: '文件无法解析' }],
      },
    })

    expect(wrapper.text()).toContain('1 个文件未能完整导入')
    expect(wrapper.text()).toContain('文件无法解析')
    await wrapper.get('footer .button').trigger('click')
    expect(wrapper.emitted('addFiles')).toHaveLength(1)
  })
})
