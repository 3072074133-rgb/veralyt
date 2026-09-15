import { describe, expect, it } from 'vitest'
import { runDuration } from './run-duration'

describe('runDuration', () => {
  const start = '2026-09-15T00:00:00Z'
  it('uses persisted start time after refresh', () => {
    expect(runDuration(start, undefined, Date.parse(start) + 125000)).toBe('02:05')
  })
  it('freezes completed runs and supports hours', () => {
    expect(runDuration(start, '2026-09-15T01:02:03Z', Date.parse(start) + 99999999)).toBe('01:02:03')
  })
  it('handles clock skew and invalid dates', () => {
    expect(runDuration(start, undefined, Date.parse(start) - 1000)).toBe('00:00')
    expect(runDuration('invalid', undefined, 0)).toBe('--:--')
  })
})
