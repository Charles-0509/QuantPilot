import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './api'

describe('api client authentication transport', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    document.cookie = 'quantpilot_csrf=; Max-Age=0; Path=/'
  })

  it('sends cookies and mirrors the CSRF cookie on writes', async () => {
    document.cookie = 'quantpilot_csrf=csrf-value; Path=/'
    const fetchMock = vi.spyOn(window, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ saved: true }),
    } as Response)

    await api('/api/risk-settings', { method: 'PUT', body: '{}' })
    expect(fetchMock).toHaveBeenCalledWith('/api/risk-settings', expect.objectContaining({
      credentials: 'include',
      headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-value' }),
    }))
  })

  it('does not attach CSRF to safe reads', async () => {
    document.cookie = 'quantpilot_csrf=csrf-value; Path=/'
    const fetchMock = vi.spyOn(window, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true }),
    } as Response)

    await api('/api/account')
    const headers = fetchMock.mock.calls[0][1]?.headers as Record<string, string>
    expect(headers['X-CSRF-Token']).toBeUndefined()
  })
})
