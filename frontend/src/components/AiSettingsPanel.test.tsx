import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import AiSettingsPanel from './AiSettingsPanel'

const { api } = vi.hoisted(() => ({ api: vi.fn() }))
vi.mock('../api', () => ({ api }))
afterEach(() => { cleanup(); api.mockReset() })

describe('AI settings', () => {
  it('fills an Anthropic preset and tests the typed values before saving', async () => {
    api.mockImplementation((path: string) => Promise.resolve(path === '/api/ai/test'
      ? { ok: true, reply: '连接成功' } : { configured: false, provider: 'openai', base_url: '', model: '', updated_at: null }))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><AiSettingsPanel /></QueryClientProvider>)
    fireEvent.click(await screen.findByText('Anthropic (Claude)'))
    fireEvent.change(document.querySelector('input[type="password"]')!, { target: { value: 'sk-ant' } })
    fireEvent.click(screen.getByText('测试连接'))
    expect(await screen.findByText(/连接成功，模型回复/)).toBeInTheDocument()
    const [, init] = api.mock.calls.find(([path]) => path === '/api/ai/test')!
    expect(JSON.parse(init.body)).toEqual({ provider: 'anthropic', base_url: 'https://api.anthropic.com', model: 'claude-sonnet-4-5', api_key: 'sk-ant' })
    expect(api.mock.calls.some(([path, options]) => path === '/api/ai/settings' && options?.method === 'PUT')).toBe(false)
  })
})
