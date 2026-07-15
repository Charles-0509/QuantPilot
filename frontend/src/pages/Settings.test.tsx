import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import SettingsPage from './Settings'

const { api } = vi.hoisted(() => ({ api: vi.fn() }))
vi.mock('../api', () => ({
  api,
  ApiError: class ApiError extends Error {},
  formatTime: (value?: string | null) => value ? `UTC+8 ${value}` : '—',
}))

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><SettingsPage /></QueryClientProvider>)
}

describe('SettingsPage', () => {
  afterEach(cleanup)

  beforeEach(() => {
    api.mockReset()
    api.mockImplementation((path: string) => {
      if (path === '/api/connection') return Promise.resolve({ configured: false, connected: false, state: 'unconfigured', paper: true, feed: 'iex', source: 'none', message: '请配置密钥' })
      if (path === '/api/connection/config') return Promise.resolve({ configured: false, paper: true, source: 'none', api_key_hint: null, feed: 'iex', updated_at: null })
      return Promise.resolve({ configured: true, paper: true, source: 'web', api_key_hint: '...ABCD', feed: 'iex', updated_at: null })
    })
  })

  it('submits Paper credentials from the settings form without showing the saved secret', async () => {
    renderPage()
    const secret = await screen.findByLabelText('API Secret Key')
    expect(secret).toHaveAttribute('type', 'password')
    fireEvent.change(screen.getByLabelText('API Key ID'), { target: { value: 'PK-ABCD' } })
    fireEvent.change(secret, { target: { value: 'paper-secret' } })
    fireEvent.click(screen.getByRole('button', { name: '验证并保存连接' }))

    await waitFor(() => expect(api).toHaveBeenCalledWith('/api/connection/config', expect.objectContaining({ method: 'PUT' })))
    const saveCall = api.mock.calls.find(([path, init]) => path === '/api/connection/config' && init?.method === 'PUT')
    expect(saveCall?.[1].body).toContain('PK-ABCD')
    expect(screen.queryByText('paper-secret')).not.toBeInTheDocument()
  })

  it('shows circuit breaker diagnostics and recovery timing', async () => {
    api.mockImplementation((path: string) => {
      if (path === '/api/connection') return Promise.resolve({
        configured: true,
        connected: false,
        state: 'circuit_open',
        paper: true,
        feed: 'iex',
        source: 'web',
        message: '连续连接失败，正在等待自动恢复',
        consecutive_failures: 5,
        last_success_at: '2026-07-15T01:00:00Z',
        last_failure_at: '2026-07-15T01:05:00Z',
        retry_at: '2026-07-15T01:06:00Z',
        last_error_category: 'tls',
      })
      if (path === '/api/connection/config') return Promise.resolve({ configured: true, paper: true, source: 'web', api_key_hint: '...ABCD', feed: 'iex', updated_at: '2026-07-15T00:00:00Z' })
      return Promise.resolve({})
    })

    renderPage()

    expect((await screen.findAllByText('Alpaca 连接保护中')).length).toBeGreaterThan(0)
    expect(screen.getByText('最近连接成功')).toBeInTheDocument()
    expect(screen.getByText('UTC+8 2026-07-15T01:00:00Z')).toBeInTheDocument()
    expect(screen.getByText('最近连接失败')).toBeInTheDocument()
    expect(screen.getByText('UTC+8 2026-07-15T01:05:00Z')).toBeInTheDocument()
    expect(screen.getByText('下次自动重试')).toBeInTheDocument()
    expect(screen.getByText('UTC+8 2026-07-15T01:06:00Z')).toBeInTheDocument()
    expect(screen.getByText('5 次')).toBeInTheDocument()
    expect(screen.getByText('TLS 安全连接')).toBeInTheDocument()
  })
})
