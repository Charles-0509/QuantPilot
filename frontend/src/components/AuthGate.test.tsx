import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import AuthGate from './AuthGate'

const mocks = vi.hoisted(() => ({ api: vi.fn(), apiForm: vi.fn() }))
vi.mock('../api', () => ({
  ...mocks,
  ApiError: class ApiError extends Error {},
}))

function renderGate() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <AuthGate><div>受保护控制台</div></AuthGate>
    </QueryClientProvider>,
  )
}

describe('AuthGate', () => {
  beforeEach(() => {
    mocks.api.mockReset()
    mocks.apiForm.mockReset()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('creates the first administrator without storing the opaque token', async () => {
    mocks.api.mockImplementation((path: string) => {
      if (path === '/api/auth/status') {
        return Promise.resolve({ setup_required: true, authenticated: false, user: null })
      }
      if (path === '/api/auth/setup') {
        return Promise.resolve({ access_token: 'opaque-token', token_type: 'bearer' })
      }
      return Promise.reject(new Error(`unexpected path ${path}`))
    })
    const storage = vi.spyOn(Storage.prototype, 'setItem')
    renderGate()

    expect(await screen.findByRole('heading', { name: '创建管理员' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('管理员用户名'), { target: { value: 'charles' } })
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'correct-horse-battery' } })
    fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'correct-horse-battery' } })
    fireEvent.click(screen.getByRole('button', { name: '创建管理员并进入系统' }))

    await waitFor(() => expect(mocks.api).toHaveBeenCalledWith(
      '/api/auth/setup',
      expect.objectContaining({ method: 'POST' }),
    ))
    expect(storage).not.toHaveBeenCalled()
  })

  it('validates weak and mismatched first-run passwords in the browser', async () => {
    mocks.api.mockResolvedValue({ setup_required: true, authenticated: false, user: null })
    renderGate()
    await screen.findByRole('heading', { name: '创建管理员' })
    fireEvent.change(screen.getByLabelText('管理员用户名'), { target: { value: 'charles' } })
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'short' } })
    fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'different' } })
    fireEvent.click(screen.getByRole('button', { name: '创建管理员并进入系统' }))
    expect(screen.getByRole('alert')).toHaveTextContent('两次输入的密码不一致')
    expect(mocks.api).toHaveBeenCalledTimes(1)

    fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'short' } })
    fireEvent.click(screen.getByRole('button', { name: '创建管理员并进入系统' }))
    expect(screen.getByRole('alert')).toHaveTextContent('管理员密码至少需要12位')
    expect(mocks.api).toHaveBeenCalledTimes(1)
  })

  it('logs in with the OAuth2 password form', async () => {
    mocks.api.mockResolvedValue({ setup_required: false, authenticated: false, user: null })
    mocks.apiForm.mockResolvedValue({ access_token: 'opaque-token', token_type: 'bearer' })
    renderGate()
    expect(await screen.findByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('管理员用户名'), { target: { value: 'charles' } })
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'correct-horse-battery' } })
    fireEvent.click(screen.getByRole('button', { name: '登录 QuantPilot' }))
    await waitFor(() => expect(mocks.apiForm).toHaveBeenCalledWith(
      '/api/auth/token',
      { username: 'charles', password: 'correct-horse-battery' },
    ))
  })

  it('returns to the login gate after a business API reports 401', async () => {
    mocks.api.mockResolvedValue({
      setup_required: false,
      authenticated: true,
      user: { username: 'charles', created_at: '2026-07-10T00:00:00Z', last_login_at: null },
    })
    renderGate()
    expect(await screen.findByText('受保护控制台')).toBeInTheDocument()
    window.dispatchEvent(new CustomEvent('quantpilot:unauthorized'))
    expect(await screen.findByRole('heading', { name: '欢迎回来' })).toBeInTheDocument()
  })
})
