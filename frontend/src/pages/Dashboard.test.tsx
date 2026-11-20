import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Dashboard from './Dashboard'

const { api } = vi.hoisted(() => ({ api: vi.fn() }))
vi.mock('../api', () => ({
  api,
  formatTime: (value?: string | null) => value ? `UTC+8 ${value}` : '—',
  money: (value: unknown) => `US$${Number(value).toFixed(2)}`,
  number: (value: unknown) => String(value),
}))

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><Dashboard /></QueryClientProvider>)
}

describe('Dashboard connection degradation', () => {
  afterEach(cleanup)

  beforeEach(() => {
    api.mockReset()
  })

  it('does not turn unavailable account, holdings, orders, or clock data into valid zero values', async () => {
    api.mockResolvedValue({
      connection: {
        configured: true,
        connected: false,
        state: 'circuit_open',
        paper: true,
        feed: 'iex',
        message: '连续连接失败，连接保护已开启',
        retry_at: '2026-07-15T01:06:00Z',
      },
      account: null,
      positions: null,
      orders: null,
      clock: null,
      availability: {
        account: 'unavailable',
        positions: 'unavailable',
        orders: 'unavailable',
        clock: 'unavailable',
      },
      portfolio: { as_of: '2026-07-15T01:05:00Z', source: 'alpaca', paper: true, status: 'unavailable', errors: ['券商快照暂不可用：ConnectionError'], checks: [] },
      engine: {
        status: 'running',
        operational_status: 'circuit_open',
        accepting_new_orders: false,
        reason: '等待 Alpaca 自动恢复',
        last_heartbeat: '2026-07-15T01:05:00Z',
      },
      events: [],
      signals: [],
    })

    renderPage()

    expect(await screen.findByText('市场状态未知')).toBeInTheDocument()
    expect(screen.getByText('持仓数据暂不可用')).toBeInTheDocument()
    expect(screen.getAllByText('开放订单状态未知').length).toBeGreaterThan(0)
    expect(screen.getByText('引擎已开启 · 等待自动恢复')).toBeInTheDocument()
    expect(screen.queryByText('美股已休市')).not.toBeInTheDocument()
    expect(screen.queryByText('暂时没有持仓')).not.toBeInTheDocument()
    expect(screen.queryByText('US$0.00')).not.toBeInTheDocument()
    expect(screen.queryByText('0 只')).not.toBeInTheDocument()
  })

  it('still renders a confirmed empty portfolio when the data is available', async () => {
    api.mockResolvedValue({
      connection: { configured: true, connected: true, state: 'connected', paper: true, feed: 'iex', message: '正常' },
      account: { equity: '100000', last_equity: '100000', buying_power: '200000' },
      positions: [],
      orders: [],
      clock: { is_open: false },
      availability: { account: 'fresh', positions: 'fresh', orders: 'fresh', clock: 'fresh' },
      portfolio: {
        as_of: '2026-07-15T01:05:00Z', source: 'alpaca', paper: true, status: 'ok', errors: [], checks: [], market_open: false,
        account: { equity: 100000, last_equity: 100000, cash: 100000, buying_power: 200000, market_value: 0, day_change: 0, day_change_pct: 0, exposure_pct: 0, trading_blocked: false },
        positions: [], orders: [],
      },
      engine: { status: 'paused', operational_status: 'paused', accepting_new_orders: false, reason: '用户暂停', last_heartbeat: null },
      events: [],
      signals: [],
    })

    renderPage()

    expect(await screen.findByText('美股已休市')).toBeInTheDocument()
    expect(screen.getByText('暂时没有持仓')).toBeInTheDocument()
    expect(screen.getByText('0 只')).toBeInTheDocument()
    expect(screen.getByText(/未成交订单 0 笔/)).toBeInTheDocument()
  })
})

describe('Dashboard snapshot consistency', () => {
  afterEach(cleanup)

  it('hides money values when the broker snapshot fails its consistency check', async () => {
    api.mockResolvedValue({
      connection: { configured: true, connected: true, state: 'connected', paper: true, feed: 'iex', message: '正常' },
      account: { equity: '7041418' }, positions: [], orders: [], clock: { is_open: true },
      portfolio: { as_of: '2026-11-12T14:45:00Z', source: 'alpaca', paper: true, status: 'suspect', errors: [],
        checks: ['净值 7,041,418.00 与现金+持仓市值 102,426.00 不一致'], market_open: true,
        account: { equity: 7041418, last_equity: 101000, cash: 83426, buying_power: 90000, market_value: 19000, day_change: 0, day_change_pct: 6871, exposure_pct: 0, trading_blocked: false },
        positions: [], orders: [] },
      engine: { status: 'running', operational_status: 'active', accepting_new_orders: true, reason: 'ok', last_heartbeat: null },
      events: [], signals: [],
    })
    renderPage()
    expect(await screen.findByText('账户数据核对未通过，金额暂不显示')).toBeInTheDocument()
    expect(screen.queryByText(/7,041,418\.00/)).toBeInTheDocument()
    expect(screen.queryByText('US$7041418.00')).not.toBeInTheDocument()
    expect(screen.getByText('持仓数据暂不可用')).toBeInTheDocument()
  })
})
