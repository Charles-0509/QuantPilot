import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import Users from './Users'

const { api } = vi.hoisted(() => ({ api: vi.fn() }))
vi.mock('../api', () => ({
  api,
  ApiError: class ApiError extends Error {},
  formatTime: () => '2026/7/10 12:00:00',
}))

const admin = { id: 1, username: 'admin', role: 'admin', is_active: true, alpaca_configured: true, created_at: '2026-07-10T00:00:00Z', last_login_at: null }

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><Users /></QueryClientProvider>)
}

describe('Users', () => {
  beforeEach(() => {
    api.mockReset()
    api.mockImplementation((path: string) => {
      if (path === '/api/auth/me') return Promise.resolve(admin)
      if (path === '/api/users') return Promise.resolve([admin])
      return Promise.resolve({})
    })
  })

  it('lets an administrator create a user without exposing Alpaca credentials', async () => {
    renderPage()
    await screen.findByRole('heading', { name: '用户管理' })
    fireEvent.change(screen.getByLabelText('新用户名'), { target: { value: 'trader-two' } })
    fireEvent.change(screen.getByLabelText('初始密码'), { target: { value: 'second-user-password' } })
    fireEvent.click(screen.getByRole('button', { name: '创建独立账户' }))

    await waitFor(() => expect(api).toHaveBeenCalledWith('/api/users', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ username: 'trader-two', password: 'second-user-password', role: 'user' }),
    })))
    expect(screen.queryByText('second-user-password')).not.toBeInTheDocument()
  })

  it('does not request the user directory for an ordinary user', async () => {
    api.mockImplementation((path: string) => {
      if (path === '/api/auth/me') return Promise.resolve({ ...admin, id: 2, role: 'user' })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    renderPage()
    expect(await screen.findByText('仅管理员可以创建和管理 QuantPilot 用户。')).toBeInTheDocument()
    expect(api).toHaveBeenCalledTimes(1)
  })
})
