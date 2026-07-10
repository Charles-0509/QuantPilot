import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Backtests from './Backtests'

const { api } = vi.hoisted(() => ({ api: vi.fn() }))
vi.mock('../api', () => ({
  api,
  ApiError: class ApiError extends Error {},
  formatTime: () => '2026/7/10 12:00:00',
  money: (value: unknown) => `$${value}`,
  number: (value: unknown) => String(value ?? 0),
}))

const strategy = {
  id: 'strategy-1',
  name: '双均线趋势',
  description: '',
  template_key: null,
  is_template: false,
  enabled: false,
  version: 1,
  created_at: '2026-07-10T00:00:00Z',
  updated_at: '2026-07-10T00:00:00Z',
  definition: {
    version: 1,
    name: '双均线趋势',
    description: '',
    symbols: ['SPY'],
    timeframe: '1Day',
    warmup_bars: 220,
    schedule: { session: 'regular', weekdays: [0, 1, 2, 3, 4] },
    entry: { type: 'group', op: 'AND', children: [] },
    exit: { type: 'group', op: 'AND', children: [] },
    position: { mode: 'percent_equity', value: 10, allow_pyramiding: false, max_additions: 1 },
    order: { type: 'market', limit_offset_bps: 10, time_in_force: 'day', stop_loss: null, take_profit: null, trailing_stop: null },
    risk: { max_symbol_pct: 10, max_positions: 8, cooldown_bars: 1 },
  },
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><Backtests /></QueryClientProvider>)
}

describe('Backtests', () => {
  beforeEach(() => {
    api.mockReset()
    api.mockImplementation((path: string, init?: RequestInit) => {
      if (path === '/api/strategies') return Promise.resolve([strategy])
      if (path === '/api/backtests' && !init) return Promise.resolve([])
      if (path === '/api/backtests' && init?.method === 'POST') {
        return Promise.resolve({
          id: 'run-1', strategy_id: 'strategy-1', status: 'queued', parameters: JSON.parse(String(init.body)), metrics: {}, error: null,
          created_at: '2026-07-10T00:00:00Z', completed_at: null,
        })
      }
      if (path === '/api/backtests/run-1') {
        return Promise.resolve({ id: 'run-1', strategy_id: 'strategy-1', status: 'queued', parameters: { symbols: ['GOOGL'], benchmark: 'QQQ' }, metrics: {}, equity_curve: [], benchmark_curve: [], trades: [], error: null, created_at: '2026-07-10T00:00:00Z', completed_at: null })
      }
      return Promise.reject(new Error(`unexpected request ${path}`))
    })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('submits an overridden stock and selectable ETF benchmark', async () => {
    renderPage()
    expect(await screen.findByRole('heading', { name: '回测实验室' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText('回测标的')).toHaveValue('SPY'))
    fireEvent.change(screen.getByLabelText('回测标的'), { target: { value: 'GOOGL' } })
    fireEvent.change(screen.getByLabelText('对比基准'), { target: { value: 'QQQ' } })
    fireEvent.click(screen.getByRole('button', { name: '运行回测' }))

    await waitFor(() => expect(api).toHaveBeenCalledWith('/api/backtests', expect.objectContaining({ method: 'POST' })))
    const call = api.mock.calls.find(([path, init]) => path === '/api/backtests' && init?.method === 'POST')
    const payload = JSON.parse(call?.[1]?.body as string)
    expect(payload.symbols).toEqual(['GOOGL'])
    expect(payload.benchmark).toBe('QQQ')
  })
})
