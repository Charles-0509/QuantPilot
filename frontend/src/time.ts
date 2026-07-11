export const SYSTEM_TIME_ZONE = 'Asia/Shanghai'

const hasExplicitZone = /(?:Z|[+-]\d{2}:?\d{2})$/i

export function parseUtcDate(value: string | number | Date): Date {
  if (value instanceof Date) return value
  if (typeof value === 'number') return new Date(value)
  const normalized = hasExplicitZone.test(value) ? value : `${value}Z`
  return new Date(normalized)
}

export function formatShanghaiDateTime(value: string | number | Date): string {
  const date = parseUtcDate(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: SYSTEM_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).format(date)
}

export function formatShanghaiDate(value: string | number | Date): string {
  const date = parseUtcDate(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: SYSTEM_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(date)
}

export function shanghaiDateOffset(days: number): string {
  const shanghaiToday = new Intl.DateTimeFormat('en-CA', {
    timeZone: SYSTEM_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date())
  const date = new Date(`${shanghaiToday}T00:00:00Z`)
  date.setUTCDate(date.getUTCDate() + days)
  return date.toISOString().slice(0, 10)
}

export function formatShanghaiChartTick(unixSeconds: number): string {
  const date = new Date(unixSeconds * 1000)
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: SYSTEM_TIME_ZONE,
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(date)
}
