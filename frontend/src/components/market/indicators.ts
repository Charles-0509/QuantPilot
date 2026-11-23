export type Bar = { timestamp: string; open: number; high: number; low: number; close: number; volume: number; feed?: string }

export const MA_LINES = [
  { period: 20, color: '#4f8cff' },
  { period: 50, color: '#f5a623' },
  { period: 100, color: '#b9c3d0' },
] as const

/** Simple moving average aligned to the input; null until `period` values exist. */
export function sma(values: number[], period: number): Array<number | null> {
  const result: Array<number | null> = []
  let sum = 0
  values.forEach((value, index) => {
    sum += value
    if (index >= period) sum -= values[index - period]
    result.push(index >= period - 1 ? sum / period : null)
  })
  return result
}
