import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import BrokerSettingsPanel from './BrokerSettingsPanel'

const { api } = vi.hoisted(() => ({ api: vi.fn() }))
vi.mock('../api', () => ({ api }))
const base = { broker: 'ibkr', trading_mode: 'paper', ibkr_gateway_url: 'https://127.0.0.1:5000/v1/api', ibkr_account_id: 'U123', ibkr_verify_tls: true, revision: 1 }
function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><BrokerSettingsPanel /></QueryClientProvider>)
  return client
}
describe('IBKR real-account consent', () => {
  afterEach(cleanup)
  beforeEach(() => {
    api.mockReset()
    let stored = { ...base }
    let challengeNumber = 0
    api.mockImplementation((path: string, init?: RequestInit) => {
      if (path === '/api/brokers/config' && !init) return Promise.resolve(stored)
      if (path.endsWith('/live-challenge')) return Promise.resolve({ challenge_token: ++challengeNumber === 1 ? 'one-use-token' : 'fresh-token', disclaimer: '真实资金可能损失，模拟收益不代表真实结果。', account_id: 'U123' })
      if (path === '/api/brokers/config' && init?.method === 'PUT') { stored = { ...stored, ...JSON.parse(String(init.body)), revision: stored.revision + 1 }; return Promise.resolve(stored) }
      return Promise.resolve({ accounts: [] })
    })
  })
  it('uses the server gateway address for an unsaved connection and account probe', async () => {
    api.mockImplementation((path: string, init?: RequestInit) => {
      if (path === '/api/brokers/config' && !init) return Promise.resolve({ ...base, broker: 'alpaca', ibkr_gateway_url: 'https://localhost:5050/v1/api', ibkr_account_id: '', revision: 0 })
      return Promise.resolve({ accounts: [] })
    })
    const client = renderPanel()
    await waitFor(() => expect(client.isFetching()).toBe(0))
    fireEvent.change(screen.getByLabelText('选择券商'), { target: { value: 'ibkr' } })
    await waitFor(() => expect(screen.getByLabelText('IBKR 网关地址')).toHaveValue('https://localhost:5050/v1/api'))
    expect(screen.getByLabelText('验证网关 TLS 证书')).toBeChecked()
    fireEvent.click(screen.getByRole('button', { name: '读取网关账户' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/api/brokers/ibkr/probe', expect.objectContaining({ method: 'POST' })))
    const probe = api.mock.calls.find(([path]) => path === '/api/brokers/ibkr/probe')
    expect(JSON.parse(String(probe?.[1].body)).ibkr_gateway_url).toBe('https://localhost:5050/v1/api')
    expect(api.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(false)
  })
  it('keeps live switch off until exact phrase and server confirmation', async () => {
    renderPanel()
    const toggle = await screen.findByLabelText('启用 IBKR 真实账户')
    fireEvent.click(toggle)
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(await screen.findByText('真实资金可能损失，模拟收益不代表真实结果。')).toBeInTheDocument()
    expect(toggle).not.toBeChecked()
    const confirm = screen.getByRole('button', { name: '确认启用真实账户' })
    expect(confirm).toBeDisabled()
    fireEvent.change(screen.getByLabelText('请输入我同意并确认'), { target: { value: '我同意' } })
    expect(confirm).toBeDisabled()
    fireEvent.change(screen.getByLabelText('请输入我同意并确认'), { target: { value: '我同意并确认' } })
    fireEvent.click(confirm)
    await waitFor(() => expect(api).toHaveBeenCalledWith('/api/brokers/config', expect.objectContaining({ method: 'PUT' })))
    const body = JSON.parse(String(api.mock.calls.find(([p, i]) => p === '/api/brokers/config' && i?.method === 'PUT')?.[1].body))
    expect(body.confirmation_text).toBe('我同意并确认')
    expect(body.challenge_token).toBe('one-use-token')
    expect(body.ibkr_account_id).toBe('U123')
  })
  it('discards confirmation when canceled and requires fresh input on reopening', async () => {
    renderPanel()
    fireEvent.click(await screen.findByLabelText('启用 IBKR 真实账户'))
    await screen.findByText('真实资金可能损失，模拟收益不代表真实结果。')
    fireEvent.change(screen.getByLabelText('请输入我同意并确认'), { target: { value: '我同意并确认' } })
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('启用 IBKR 真实账户'))
    await screen.findByText('真实资金可能损失，模拟收益不代表真实结果。')
    expect(screen.getByLabelText('请输入我同意并确认')).toHaveValue('')
    expect(screen.getByRole('button', { name: '确认启用真实账户' })).toBeDisabled()
    expect(api.mock.calls.filter(([p]) => p.endsWith('/live-challenge'))).toHaveLength(2)
  })
  it('requires a new modal and phrase after live is switched off and another paper account is selected', async () => {
    renderPanel()
    fireEvent.click(await screen.findByLabelText('启用 IBKR 真实账户'))
    await screen.findByText('真实资金可能损失，模拟收益不代表真实结果。')
    fireEvent.change(screen.getByLabelText('请输入我同意并确认'), { target: { value: '我同意并确认' } })
    fireEvent.click(screen.getByRole('button', { name: '确认启用真实账户' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await waitFor(() => expect(screen.getByLabelText('启用 IBKR 真实账户')).toBeChecked())
    fireEvent.click(screen.getByLabelText('启用 IBKR 真实账户'))
    await waitFor(() => expect(screen.getByLabelText('启用 IBKR 真实账户')).not.toBeChecked())
    await waitFor(() => expect(screen.getByLabelText('IBKR 账户ID')).toHaveValue(''))
    fireEvent.change(screen.getByLabelText('IBKR 账户ID'), { target: { value: 'DU999' } })
    fireEvent.click(screen.getByRole('button', { name: '保存券商与账户' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '保存券商与账户' })).not.toBeDisabled())
    fireEvent.change(screen.getByLabelText('IBKR 账户ID'), { target: { value: 'U123' } })
    fireEvent.click(screen.getByLabelText('启用 IBKR 真实账户'))
    await screen.findByText('真实资金可能损失，模拟收益不代表真实结果。')
    expect(screen.getByLabelText('请输入我同意并确认')).toHaveValue('')
    expect(screen.getByRole('button', { name: '确认启用真实账户' })).toBeDisabled()
    expect(api.mock.calls.filter(([p]) => p.endsWith('/live-challenge'))).toHaveLength(2)
  })

})
