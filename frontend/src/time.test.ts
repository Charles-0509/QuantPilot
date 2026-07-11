import { describe, expect, it, vi } from 'vitest'
import { formatShanghaiChartTick, formatShanghaiDateTime, parseUtcDate, shanghaiDateOffset } from './time'

describe('UTC+8 time formatting', () => {
  it('treats timezone-less backend timestamps as UTC and displays Asia/Shanghai', () => {
    expect(formatShanghaiDateTime('2026-07-10T19:01:10')).toContain('2026/07/11 03:01:10')
  })

  it('uses UTC+8 for chart ticks and timestamp parsing', () => {
    const epoch = Math.floor(parseUtcDate('2026-07-10T13:30:00Z').getTime() / 1000)
    expect(formatShanghaiChartTick(epoch)).toContain('21:30')
  })

  it('builds date presets from the Shanghai calendar day', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-07-10T17:00:00Z'))
    expect(shanghaiDateOffset(0)).toBe('2026-07-11')
    expect(shanghaiDateOffset(-1)).toBe('2026-07-10')
    vi.useRealTimers()
  })
})
