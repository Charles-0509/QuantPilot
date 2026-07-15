import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Shell from './Shell'

const { api } = vi.hoisted(() => ({ api: vi.fn() }))
vi.mock('../api', () => ({ api }))

class MockWebSocket {
  onopen: (() => void) | null = null
  onmessage: ((message: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null

  close() {}
}

function renderShell() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Routes>
          <Route element={<Shell />}>
            <Route index element={<div>控制台内容</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('Shell connection status', () => {
  beforeEach(() => {
    vi.stubGlobal('WebSocket', MockWebSocket)
    api.mockReset()
    api.mockImplementation((path: string) => {
      if (path === '/api/connection') return Promise.resolve({
        configured: true,
        connected: false,
        state: 'degraded',
        paper: true,
        feed: 'iex',
        message: 'TLS连接暂时不稳定',
      })
      if (path === '/api/auth/me') return Promise.resolve({
        id: 1,
        username: 'charles',
        role: 'admin',
        is_active: true,
        alpaca_configured: true,
        created_at: '2026-07-15T00:00:00Z',
        last_login_at: null,
      })
      return Promise.resolve({})
    })
  })

  afterEach(() => vi.unstubAllGlobals())

  it('distinguishes a configured but degraded connection from a missing API key', async () => {
    renderShell()

    const label = await screen.findByText('Alpaca 连接不稳定')
    expect(label.closest('.badge')).toHaveClass('badge-warning')
    expect(label).toHaveAttribute('title', 'TLS连接暂时不稳定')
    expect(screen.queryByText('等待 Alpaca 密钥')).not.toBeInTheDocument()
    expect(screen.getByText('QuantPilot 服务在线')).toBeInTheDocument()
  })
})
