import { expect, test } from '@playwright/test'

test('composer stops a running request and preserves the draft', async ({ page }, testInfo) => {
  const created = await page.request.post('/api/v1/tasks')
  const { id } = await created.json()
  const snapshot = await (await page.request.get(`/api/v1/tasks/${id}`)).json()
  let stopped = false
  await page.route(`**/api/v1/tasks/${id}`, (route) => route.fulfill({ json: {
    ...snapshot, status: stopped ? 'cancelled' : 'executing', pending_run_id: stopped ? null : 'test-run',
  } }))
  await page.route(`**/api/v1/tasks/${id}/events`, (route) => route.fulfill({ contentType: 'text/event-stream', body: '' }))
  await page.route(`**/api/v1/tasks/${id}/runs/test-run`, (route) => {
    expect(route.request().method()).toBe('DELETE')
    stopped = true
    return route.fulfill({ status: 204 })
  })
  await page.goto(`/tasks/${id}`)
  await expect(page.locator('.progress-panel')).toHaveCount(0)
  await page.locator('textarea').fill('下一条问题')
  await expect(page.getByRole('button', { name: '中止分析', exact: true })).toBeEnabled()
  await page.screenshot({ path: testInfo.outputPath('composer-stop.png'), fullPage: true })
  await page.getByRole('button', { name: '中止分析', exact: true }).click()
  await expect(page.getByRole('button', { name: '发送消息', exact: true })).toBeEnabled()
  await expect(page.locator('textarea')).toHaveValue('下一条问题')
  expect(stopped).toBe(true)
})

test('sends the first message without uploading a file', async ({ page }) => {
  await page.goto('/')
  const composer = page.locator('textarea')
  await composer.fill('你好')
  await expect(page.locator('.send-button')).toBeEnabled()
  const submitted = page.waitForResponse((response) => response.url().endsWith('/messages') && response.request().method() === 'POST')
  await composer.press('Enter')
  expect((await submitted).ok()).toBe(true)
  await expect(page).toHaveURL(/\/tasks\/[0-9a-f-]+$/)
  await expect(page.locator('.user-message')).toHaveText('你好')
  await expect(composer).toHaveValue('')
  await expect(page.locator('.empty-state')).toHaveCount(0)
})

test('workbench and libraries fit the desktop viewport', async ({ page }, testInfo) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '把表格交给我，直接说你想分析什么' })).toBeVisible()
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('workbench.png'), fullPage: true })

  await expect(page.getByRole('region', { name: '历史会话', exact: true })).toBeVisible()
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('history.png'), fullPage: true })

  await page.goto('/datasets')
  await expect(page.getByRole('heading', { name: '数据集', exact: true })).toBeVisible()
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)

  await page.goto('/reports')
  await expect(page.getByRole('heading', { name: '报告库', exact: true })).toBeVisible()
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('selecting a file lazily creates a task and imports it', async ({ page }, testInfo) => {
  await page.goto('/')
  await expect(page).toHaveURL(/\/$/)
  await page.locator('input[type="file"]').setInputFiles({
    name: 'finance.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from('月份,收入\n2026-01,100\n2026-02,120\n', 'utf8'),
  })
  await expect(page).toHaveURL(/\/tasks\/[0-9a-f-]+$/)
  await page.getByRole('button', { name: /数据来源/ }).click()
  await expect(page.getByRole('heading', { name: '数据来源' })).toBeVisible()
  await expect(page.getByText('finance.csv', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('已就绪', { exact: true })).toBeVisible()
  await expect.poll(() => page.locator('.source-drawer').evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('data-source-drawer.png'), fullPage: true })
  await page.keyboard.press('Escape')
  await page.locator('.content').evaluate((element: HTMLElement) => { element.style.minHeight = '1800px' })
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight))
  await expect(page.getByRole('button', { name: /数据来源/ })).toBeVisible()
  const taskUrl = page.url()
  const history = page.getByRole('region', { name: '历史会话' })
  const taskHistoryLink = history.locator(`a[href="${new URL(taskUrl).pathname}"]`)
  await expect(taskHistoryLink).toBeVisible()
  await page.getByRole('link', { name: '当前分析' }).click()
  await expect(page).toHaveURL(taskUrl)
  await expect(page.getByRole('button', { name: /数据来源/ })).toBeVisible()

  await page.getByRole('button', { name: '新建分析', exact: true }).click()
  await expect(page.getByRole('heading', { name: '把表格交给我，直接说你想分析什么' })).toBeVisible()
  await taskHistoryLink.click()
  await expect(page).toHaveURL(taskUrl)

  await page.goto(taskUrl)
  await page.getByRole('button', { name: /数据来源/ }).click()
  await page.getByRole('button', { name: '预览与纠错' }).click()
  await expect(page.getByRole('heading', { name: '数据预览与纠错' })).toBeVisible()
  await expect(page.locator('.correction-grid')).toBeVisible()
  await page.getByRole('button', { name: '质量', exact: true }).click()
  await expect(page.getByText('重复行', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '字段', exact: true }).click()
  await expect(page.getByText('语义类型', { exact: true })).toBeVisible()
  await expect.poll(() => page.locator('.dataset-workspace').evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('dataset-workspace.png'), fullPage: true })
  await page.getByRole('button', { name: '关闭' }).click()
  await expect(taskHistoryLink).toContainText('finance.csv')
})

test('completed analysis renders interactive charts', async ({ page }, testInfo) => {
  const acceptedTask = process.env.ACCEPTED_TASK_ID!
  await page.goto(`/tasks/${acceptedTask}`)
  await expect(page.getByText('关键发现', { exact: true })).toBeVisible()
  const canvases = page.locator('.analysis-chart canvas')
  await expect(canvases.first()).toBeVisible()
  const pixels = await canvases.first().evaluate((canvas: HTMLCanvasElement) => {
    const context = canvas.getContext('2d')
    if (!context) return 0
    const data = context.getImageData(0, 0, canvas.width, canvas.height).data
    let colored = 0
    for (let index = 0; index < data.length; index += 4) {
      if (data[index + 3] && (data[index] < 245 || data[index + 1] < 245 || data[index + 2] < 245)) colored += 1
    }
    return colored
  })
  expect(pixels).toBeGreaterThan(100)

  const pngDownload = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载 PNG' }).first().click()
  await expect((await pngDownload).suggestedFilename()).toBe('月度收入趋势.png')

  await page.getByRole('button', { name: '查看图表数据' }).first().click()
  await expect(page.locator('.chart-table-view').first()).toBeVisible()
  await expect(page.getByRole('cell', { name: '2026-01' }).first()).toBeVisible()
  const csvDownload = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载 CSV' }).first().click()
  await expect((await csvDownload).suggestedFilename()).toBe('月度收入趋势.csv')
  await page.screenshot({ path: testInfo.outputPath('chart-data-view.png'), fullPage: true })

  await page.getByRole('button', { name: '查看图表' }).first().click()
  await expect(canvases.first()).toBeVisible()
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('multi-sheet workbook shows imported and skipped sheet counts', async ({ page }, testInfo) => {
  const created = await page.request.post('/api/v1/tasks')
  const uploadTask = (await created.json() as { id: string }).id
  await page.goto(`/tasks/${uploadTask}`)
  await page.locator('input[type="file"]').setInputFiles(process.env.E2E_MULTI_SHEET_PATH!)
  await expect(page.getByText('已读取 18 个数据表')).toBeVisible()
  await page.getByRole('button', { name: /数据来源/ }).click()
  await expect(page.getByText('工作表', { exact: true })).toBeVisible()
  const fileCard = page.locator('.source-file-card').filter({ hasText: 'e2e-multi-sheet.xlsx' })
  await expect(fileCard.getByText('18', { exact: true }).first()).toBeVisible()
  await expect(page.getByText(/已跳过 1 个隐藏或空白工作表/)).toBeVisible()
  await expect(page.getByText('已就绪', { exact: true })).toBeVisible()
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('multi-sheet-import.png'), fullPage: true })
})
