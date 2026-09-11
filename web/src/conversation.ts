import type { MessageRecord, WorkflowRun } from './types'

export interface ConversationTurn {
  user: MessageRecord
  assistantMessages: MessageRecord[]
  run?: WorkflowRun
}

const retryAnalysisPattern = /^(?:请|麻烦|帮我)?(?:(?:重新|再|继续)(?:分析|计算|统计|汇总)(?:一下|一次|一遍|数据|这份数据|当前数据)?|重做分析|(?:重新|再)跑(?:一下|一次|一遍)?)$/

function isRetryAnalysisRequest(content: string) {
  return retryAnalysisPattern.test(content.replace(/[\s，。！？,.!?、；;：:]/g, '').toLocaleLowerCase())
}

export function resolveRetryQuestion(turn: ConversationTurn, turns: ConversationTurn[]) {
  const current = turn.user.content.trim()
  if (!isRetryAnalysisRequest(current)) return current
  const index = turns.findIndex((item) => item.user.id === turn.user.id)
  for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
    const candidate = turns[cursor].user.content.trim()
    if (candidate && !isRetryAnalysisRequest(candidate)) return candidate
  }
  return current
}

export function buildConversationTurns(
  messages: MessageRecord[],
  runs: WorkflowRun[],
  pendingRunId?: string,
  activeRunId?: string,
): ConversationTurn[] {
  const orderedMessages = [...messages].sort((left, right) => left.sequence - right.sequence)
  const userMessages = orderedMessages.filter((message) => message.role === 'user')

  return userMessages.map((user, index) => {
    const nextUserSequence = userMessages[index + 1]?.sequence ?? Number.POSITIVE_INFINITY
    const assistantMessages = orderedMessages.filter((message) => (
      message.role === 'assistant'
      && message.sequence > user.sequence
      && message.sequence < nextUserSequence
    ))
    const candidates = runs
      .filter((run) => run.message_sequence === user.sequence)
      .sort((left, right) => right.started_at.localeCompare(left.started_at))
    const run = candidates.find((item) => item.id === pendingRunId)
      ?? candidates.find((item) => item.id === activeRunId)
      ?? candidates[0]

    return { user, assistantMessages, run }
  })
}
