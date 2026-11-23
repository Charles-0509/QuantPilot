import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import MarketDataSourcePanel from './MarketDataSourcePanel'

const { api } = vi.hoisted(() => ({ api: vi.fn() }))
vi.mock('../api', () => ({ api }))
afterEach(() => { cleanup(); api.mockReset() })

const info = { source: 'ibkr', label: 'IBKR 行情', live: '取决于订阅', history: 'IBKR 历史数据' }

function show(broker: 'alpaca' | 'ibkr') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><MarketDataSourcePanel broker={broker} configuredSource="ibkr" /></QueryClientProvider>)
}

describe('market data source', () => {
  it('lets an IBKR user switch quotes and bars to Alpaca without touching broker settings', async () => {
    api.mockResolvedValue(info)
    show('ibkr')
    fireEvent.change(await screen.findByLabelText('行情来源'), { target: { value: 'alpaca' } })
    await waitFor(() => expect(api).toHaveBeenCalledWith('/api/brokers/market-data', { method: 'PUT', body: JSON.stringify({ source: 'alpaca' }) }))
    expect(api.mock.calls.some(([path]) => path === '/api/brokers/config')).toBe(false)
  })

  it('has no selector when Alpaca is already the broker', async () => {
    api.mockResolvedValue({ ...info, source: 'alpaca', label: 'Alpaca 免费行情' })
    show('alpaca')
    expect(await screen.findByText(/当前券商为 Alpaca/)).toBeInTheDocument()
    expect(screen.queryByLabelText('行情来源')).not.toBeInTheDocument()
  })
})
