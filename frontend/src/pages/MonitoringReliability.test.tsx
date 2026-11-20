import { act, cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import Risk from './Risk'
import Strategies from './Strategies'
import Market from './Market'

const { api } = vi.hoisted(() => ({ api: vi.fn() }))
vi.mock('../api', () => ({ api, formatTime: (value: string) => value || '—' }))
vi.mock('../components/CandleChart', () => ({ default: () => <div>chart</div> }))
afterEach(() => { cleanup(); vi.useRealTimers(); api.mockReset() })
function show(component: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<MemoryRouter><QueryClientProvider client={client}>{component}</QueryClientProvider></MemoryRouter>)
  return client
}

describe('monitoring reliability', () => {
  it('shows the risk request failure instead of loading forever', async () => {
    api.mockRejectedValue(new Error('请求失败 (530)'))
    show(<Risk />)
    expect(await screen.findByText('请求失败 (530)')).toBeInTheDocument()
    expect(screen.queryByText('正在加载风险闸门')).not.toBeInTheDocument()
  })
  it('does not equate an enabled strategy with a running engine', async () => {
    api.mockResolvedValue([{ id: 's', name: 'Momentum Breakout', enabled: true, is_template: false,
      description: '', definition: { symbols: ['NVDA'], timeframe: '15Min' } }])
    show(<Strategies />)
    expect(await screen.findByText('Momentum Breakout')).toBeInTheDocument()
    expect(screen.queryByText('运行中')).not.toBeInTheDocument()
    expect(screen.getAllByText('已启用').length).toBeGreaterThan(0)
  })
  it('shows bar time and schedules refreshing rather than implying an old quote is live', async () => {
    api.mockImplementation((path: string) => Promise.resolve(path.includes('watchlist') ? ['NVDA'] : [
      { timestamp: '2026-11-04T18:30:00Z', open: 100, high: 102, low: 99, close: 101, volume: 10 },
    ]))
    vi.useFakeTimers()
    show(<Market />)
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(screen.getByText(/2026-11-04T18:30:00Z/)).toBeInTheDocument()
    const reads = () => api.mock.calls.filter(([path]) => path.includes('/api/market/bars')).length
    expect(reads()).toBe(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(15000) })
    expect(reads()).toBeGreaterThan(1)
  })
})
