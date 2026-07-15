import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
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
      if (path.startsWith('/api/orders')) return Promise.reject(new Error('暂时无法确认开放订单'))
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
})
