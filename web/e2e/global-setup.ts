import { spawnSync } from 'node:child_process'
import path from 'node:path'

export default async function globalSetup() {
  const projectRoot = path.resolve('..')
  const python = path.join(projectRoot, '.venv', 'Scripts', 'python.exe')
  const result = spawnSync(python, ['tests/support/prepare_e2e.py'], {
    cwd: projectRoot,
    encoding: 'utf8',
    env: {
      ...process.env,
      PYTHONPATH: projectRoot,
      PYTHONIOENCODING: 'utf-8',
      PYTHONUTF8: '1',
      ANALYSE_AGENT_MAX_SHEETS: '20',
    },
  })
  if (result.status !== 0) {
    throw new Error(`E2E fixture setup failed: ${result.stderr || result.stdout}`)
  }
  const fixture = JSON.parse(result.stdout.trim()) as { task_id: string; workbook_path: string }
  process.env.ACCEPTED_TASK_ID = fixture.task_id
  process.env.REPLAY_TASK_ID = fixture.task_id
  process.env.E2E_MULTI_SHEET_PATH = fixture.workbook_path
}
