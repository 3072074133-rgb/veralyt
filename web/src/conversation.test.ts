import { describe, expect, it } from 'vitest'
import type { MessageRecord, WorkflowRun } from './types'
import { buildConversationTurns, resolveRetryQuestion } from './conversation'

function message(id: string, role: MessageRecord['role'], sequence: number, content = id): MessageRecord {
  return { id, role, sequence, content, created_at: `2026-09-11T00:00:0${sequence}Z` }
}

function run(id: string, messageSequence: number, status = 'completed', isActive = false): WorkflowRun {
  return {
    id,
    task_id: 'task-1',
    question: id,
    status,
    is_active: isActive,
    data_revision: 1,
    message_sequence: messageSequence,
    attempt_count: 0,
    entry_node: 'classify',
    started_at: `2026-09-11T00:01:0${messageSequence}Z`,
  }
}

describe('buildConversationTurns', () => {
  it('keeps each user message with its own assistant response and run', () => {
    const turns = buildConversationTurns(
      [message('user-1', 'user', 1), message('assistant-1', 'assistant', 2), message('user-2', 'user', 3), message('assistant-2', 'assistant', 4)],
      [run('run-2', 3, 'failed'), run('run-1', 1, 'completed', true)],
      undefined,
      'run-1',
    )

    expect(turns.map((turn) => turn.user.id)).toEqual(['user-1', 'user-2'])
    expect(turns.map((turn) => turn.run?.id)).toEqual(['run-1', 'run-2'])
    expect(turns[0].assistantMessages.map((item) => item.id)).toEqual(['assistant-1'])
    expect(turns[1].assistantMessages.map((item) => item.id)).toEqual(['assistant-2'])
  })

  it('prefers the pending run for a turn while it is queued', () => {
    const turns = buildConversationTurns(
      [message('user-1', 'user', 1)],
      [run('older-run', 1, 'completed', true), run('pending-run', 1, 'queued')],
      'pending-run',
      'older-run',
    )

    expect(turns[0].run?.id).toBe('pending-run')
  })

  it('retries the latest substantive question instead of another retry command', () => {
    const turns = buildConversationTurns(
      [
        message('user-1', 'user', 1, '分析报表，统计各部门盈亏'),
        message('user-2', 'user', 2, '重新分析'),
        message('user-3', 'user', 3, '再分析一次'),
      ],
      [run('run-1', 1), run('run-2', 2, 'failed'), run('run-3', 3, 'failed')],
    )

    expect(resolveRetryQuestion(turns[2], turns)).toBe('分析报表，统计各部门盈亏')
    expect(resolveRetryQuestion(turns[0], turns)).toBe('分析报表，统计各部门盈亏')
  })
})
