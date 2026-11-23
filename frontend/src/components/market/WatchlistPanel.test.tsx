import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import WatchlistPanel from './WatchlistPanel'
import type { WatchlistGroup } from '../../types'

const { api } = vi.hoisted(() => ({ api: vi.fn() }))
vi.mock('../../api', () => ({ api }))
afterEach(() => { cleanup(); api.mockReset(); localStorage.clear(); vi.restoreAllMocks() })

const groups: WatchlistGroup[] = [
  { id: 1, name: 'ETF', position: 0, symbols: ['QQQ', 'VOO'] },
  { id: 2, name: '股票', position: 1, symbols: ['GOOGL'] },
]

function show(onSelect = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}>
    <WatchlistPanel groups={groups} quotes={{ QQQ: { symbol: 'QQQ', price: 500, change: -5, change_pct: -0.99, session: 'regular' } as never }}
      selected="QQQ" onSelect={onSelect} />
  </QueryClientProvider>)
  return { client, onSelect }
}

describe('WatchlistPanel', () => {
  it('renders groups with quotes and selects a symbol on click', () => {
    const { onSelect } = show()
    expect(screen.getByText('ETF')).toBeInTheDocument()
    expect(screen.getByText('500.00')).toBeInTheDocument()
    expect(screen.getByText('-0.99%')).toHaveClass('negative')
    fireEvent.click(screen.getByText('GOOGL'))
    expect(onSelect).toHaveBeenCalledWith('GOOGL')
  })

  it('renames a group through PATCH', async () => {
    api.mockResolvedValue(groups)
    show()
    fireEvent.click(screen.getByLabelText('重命名ETF'))
    const input = screen.getByLabelText('新分组名称')
    fireEvent.change(input, { target: { value: '指数基金' } })
    fireEvent.click(screen.getByLabelText('保存分组名称'))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/api/watchlist/groups/1', { method: 'PATCH', body: JSON.stringify({ name: '指数基金' }) }))
  })

  it('asks before deleting a group and says where its symbols go', () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    show()
    fireEvent.click(screen.getByLabelText('删除ETF'))
    expect(confirm).toHaveBeenCalledWith('删除分组“ETF”，其中 2 只股票会移到“股票”？')
    expect(api).not.toHaveBeenCalled()
  })

  it('moves a symbol to another group from the row menu', async () => {
    api.mockResolvedValue(groups)
    show()
    fireEvent.click(screen.getByLabelText('VOO操作'))
    fireEvent.click(screen.getByRole('menuitem', { name: '移到“股票”' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/api/watchlist/items/VOO', { method: 'PATCH', body: JSON.stringify({ group_id: 2 }) }))
  })

  it('collapses a group and remembers it', () => {
    show()
    fireEvent.click(screen.getByLabelText('收起ETF'))
    expect(screen.queryByText('VOO')).not.toBeInTheDocument()
    expect(JSON.parse(localStorage.getItem('qp-watchlist-collapsed') || '[]')).toEqual([1])
  })
})
