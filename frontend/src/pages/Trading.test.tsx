import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Trading from './Trading'

const { api } = vi.hoisted(() => ({ api: vi.fn() }))
vi.mock('../api', () => ({
  api,
  formatTime: (value?: string | null) => value || '—',
  money: (value: unknown) => String(value),
}))

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><Trading /></QueryClientProvider>)
}

describe('Trading operational status', () => {
  afterEach(cleanup)
  beforeEach(() => {
    api.mockReset()
  })

  it('shows enabled strategies as waiting and exposes an orders error while the engine is degraded', async () => {
    api.mockImplementation((path: string) => {
      if (path === '/api/engine') return Promise.resolve({
        status: 'running',
        operational_status: 'degraded',
        accepting_new_orders: false,
        reason: 'Alpaca 连接不稳定，正在自动重试',
        last_heartbeat: '2026-07-15T01:05:00Z',
      })
      if (path === '/api/strategies') return Promise.resolve([{
        id: 'strategy-1',
        name: '测试策略',
        enabled: true,
        definition: { symbols: ['SPY'], timeframe: '15Min' },
      }])
      if (path.startsWith('/api/signals')) return Promise.resolve([])
      if (path.startsWith('/api/portfolio')) return Promise.reject(new Error('暂时无法确认开放订单'))
      return Promise.resolve({})
    })

    renderPage()

    expect(await screen.findByText('引擎已开启 · 连接不稳定')).toBeInTheDocument()
    expect(screen.getByText('策略反应堆等待连接稳定')).toBeInTheDocument()
    expect(screen.getByText('ENABLED / WAITING')).toBeInTheDocument()
    expect(screen.queryByText('ACTIVE')).not.toBeInTheDocument()
    expect(await screen.findByText('暂时无法确认开放订单')).toBeInTheDocument()
    expect(screen.queryByText('没有开放订单')).not.toBeInTheDocument()
  })

  it('shows the actual partial fill instead of only submitted status', async () => {
    api.mockImplementation((path: string) => {
      if (path === '/api/engine') return Promise.resolve({ status: 'paused', operational_status: 'paused', reason: '执行安全隔离' })
      if (path.startsWith('/api/signals')) return Promise.resolve([{ id: 's', symbol: 'SPY', action: 'buy', price: 100,
        status: 'submitted', execution_status: 'partially_filled_canceled', filled_qty: 12, reason: '突破' }])
      return Promise.resolve([])
    })
    renderPage()
    expect(await screen.findByText('部分成交后撤单/到期')).toBeInTheDocument()
    expect(screen.getByText('12')).toBeInTheDocument()
  })
})
