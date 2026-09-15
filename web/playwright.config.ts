import { defineConfig, devices } from '@playwright/test'
import os from 'node:os'
import path from 'node:path'

const widths = [1024, 1366, 1440, 1920]
const port = 8011
const dataDir = path.join(os.tmpdir(), `analyse-agent-e2e-${process.pid}`)

export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/global-setup.ts',
  timeout: 30_000,
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    channel: 'chrome',
    screenshot: 'only-on-failure',
  },
  projects: widths.map((width) => ({
    name: `desktop-${width}`,
    use: { ...devices['Desktop Chrome'], viewport: { width, height: 900 } },
  })),
  webServer: {
    command: `.venv\\Scripts\\python.exe -m uvicorn main:app --host 127.0.0.1 --port ${port}`,
    cwd: '..',
    url: `http://127.0.0.1:${port}/api/health`,
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      ...process.env,
      ANALYSE_AGENT_PORT: String(port),
      ANALYSE_AGENT_DATA_DIR: dataDir,
      ANALYSE_AGENT_MAX_SHEETS: '20',
    },
  },
})

process.env.ANALYSE_AGENT_DATA_DIR = dataDir
process.env.ANALYSE_AGENT_MAX_SHEETS = '20'
