export function runDuration(start: string, end: string | undefined, now: number): string {
  const elapsed = (end ? Date.parse(end) : now) - Date.parse(start)
  if (!Number.isFinite(elapsed)) return '--:--'
  const seconds = Math.max(0, Math.floor(elapsed / 1000))
  const pad = (value: number) => String(value).padStart(2, '0')
  const minutes = Math.floor(seconds / 60)
  return minutes >= 60
    ? `${pad(Math.floor(minutes / 60))}:${pad(minutes % 60)}:${pad(seconds % 60)}`
    : `${pad(minutes)}:${pad(seconds % 60)}`
}
