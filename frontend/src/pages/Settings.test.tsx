import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SettingsPage from './Settings'

const { api } = vi.hoisted(() => ({ api: vi.fn() }))
vi.mock('../api', () => ({
  api,
  ApiError: class ApiError extends Error {},
  formatTime: () => '2026/7/10 12:00:00',
}))

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><SettingsPage /></QueryClientProvider>)
}

describe('SettingsPage', () => {
  beforeEach(() => {
    api.mockReset()
    api.mockImplementation((path: string) => {
      if (path === '/api/connection') return Promise.resolve({ configured: false, connected: false, paper: true, feed: 'iex', source: 'none', message: '请配置密钥' })
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
})
