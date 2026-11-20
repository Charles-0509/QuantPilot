import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import ExecutionIncidents from './ExecutionIncidents'

afterEach(cleanup)

describe('execution snapshot evidence', () => {
  it('shows the trigger position and remaining quantity, including contained incidents', () => {
    render(<ExecutionIncidents incidents={[{
      id: 4, symbol: 'NVDA', status: 'contained', reason: '开放卖单数量超过实际多头持仓', updated_at: '2026-10-27T14:00:45Z',
      details: { actual_qty: 2, owned_qty: 4, reserved_sell_qty: 4,
        positions_observed_at: '2026-10-27T14:00:45Z', orders_observed_at: '2026-10-27T14:00:44Z',
        snapshot_completed_at: '2026-10-27T14:00:45Z', snapshot_attempts: 3,
        open_orders: [{ id: '632c97e7', status: 'partially_filled', qty: 6, filled_qty: 2, remaining_qty: 4 }] },
    }]} />)
    fireEvent.click(screen.getByText(/NVDA · 已控制/))
    expect(screen.getByText(/实际持仓 2 股 · 策略归属 4 股 · 卖单预留 4 股/)).toBeInTheDocument()
    expect(screen.getByText('632c97e7')).toBeInTheDocument()
    expect(screen.getByText('剩余量')).toBeInTheDocument()
    expect(screen.getByText(/核对 3 次/)).toBeInTheDocument()
  })

  it('does not pretend an unavailable snapshot is an empty account', () => {
    render(<ExecutionIncidents incidents={[{ id: 5, symbol: 'NVDA', status: 'active', reason: '外部卖出', updated_at: '',
      details: { actual_qty: null, open_orders: null, snapshot_error: 'ConnectionError' } }]} />)
    expect(screen.getByText(/实际持仓 未知 股/)).toBeInTheDocument()
    expect(screen.getByText(/快照读取失败：ConnectionError/)).toBeInTheDocument()
    expect(screen.queryByText('当时没有开放卖单')).not.toBeInTheDocument()
  })

  it('derives remaining quantity for old incidents without changing stored ownership', () => {
    render(<ExecutionIncidents incidents={[{ id: 4, symbol: 'NVDA', status: 'contained', reason: '旧记录', updated_at: '',
      details: { actual_qty: 2, reserved_sell_qty: 4, open_orders: [{ id: '632c97e7', status: 'partially_filled', qty: 6, filled_qty: 2 }] } }]} />)
    const row = screen.getByText('632c97e7').closest('tr')!
    expect(row.lastElementChild).toHaveTextContent('4')
    expect(screen.queryByText(/等待人工确认启动/)).not.toBeInTheDocument()
  })
})
