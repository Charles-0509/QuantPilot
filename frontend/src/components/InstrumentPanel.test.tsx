import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import InstrumentPanel, { tradeKind, withTradeKind } from './InstrumentPanel'
import type { RuleDefinition } from '../types'

afterEach(cleanup)

const base = {
  version: 1, name: 't', description: '', symbols: ['SPY'], timeframe: '1Day', warmup_bars: 60,
  schedule: { session: 'regular', weekdays: [0, 1, 2, 3, 4] },
  entry: { type: 'group', op: 'AND', children: [] }, exit: { type: 'group', op: 'AND', children: [] },
  position: { mode: 'risk_based', value: 1, allow_pyramiding: false, max_additions: 1 },
  order: { type: 'market', limit_offset_bps: 10, time_in_force: 'gtc', stop_loss: { mode: 'percent', value: 2, atr_period: 14 },
    take_profit: { mode: 'percent', value: 4, atr_period: 14 }, trailing_stop: { mode: 'percent', value: 3 } },
  risk: { max_symbol_pct: 10, max_positions: 8, cooldown_bars: 1 },
} as unknown as RuleDefinition

describe('InstrumentPanel', () => {
  it('defaults legacy definitions to long stock', () => {
    expect(tradeKind(base)).toBe('long')
  })

  it('clears settings the backend rejects when switching kind', () => {
    const short = withTradeKind(base, 'short')
    expect(short).toMatchObject({ direction: 'short', instrument: 'stock', option: null })
    expect(short.order.trailing_stop).toBeNull()
    expect(short.order.stop_loss).not.toBeNull()

    const option = withTradeKind(base, 'option')
    expect(option).toMatchObject({ direction: 'long', instrument: 'option' })
    expect(option.option?.right).toBe('call')
    expect(option.order).toMatchObject({ stop_loss: null, take_profit: null, trailing_stop: null, time_in_force: 'day' })
    expect(option.position.mode).toBe('percent_equity')

    const back = withTradeKind(option, 'short')
    expect(back.order.stop_loss).toMatchObject({ mode: 'percent', value: 2 })
    expect(back.order.take_profit).toMatchObject({ mode: 'percent', value: 4 })
  })

  it('edits option parameters', () => {
    const onChange = vi.fn()
    render(<InstrumentPanel definition={withTradeKind(base, 'option')} onChange={onChange} />)
    fireEvent.change(screen.getByLabelText('合约类型'), { target: { value: 'put' } })
    expect(onChange.mock.calls.at(-1)?.[0].option.right).toBe('put')
    fireEvent.click(screen.getByRole('radio', { name: '股票做空' }))
    expect(onChange.mock.calls.at(-1)?.[0].direction).toBe('short')
  })
})
