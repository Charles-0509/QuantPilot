import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import Performance from './Performance'
import StrategyAssistant from '../components/StrategyAssistant'

const { api } = vi.hoisted(() => ({ api: vi.fn() }))
vi.mock('../api', () => ({ api, formatTime: (value: string) => value || '—', money: (value: unknown) => `US$${Number(value).toFixed(2)}` }))
vi.mock('../components/EquityChart', () => ({ default: () => <div>curve</div> }))
afterEach(() => { cleanup(); api.mockReset() })

function show(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter><QueryClientProvider client={client}>{node}</QueryClientProvider></MemoryRouter>)
}

const base = { enabled: true, symbols: ['SPY'], unrealized_pnl: 0, wins: 0, losses: 0, avg_win: null, avg_loss: null, profit_factor: null,
  max_drawdown: 0, avg_holding_minutes: 30, curve: [], open_lots: [], ownership_cleared_qty: 0, unmatched_sell_qty: 0, first_trade_at: null }

describe('strategy performance', () => {
  it('ranks strategies and explains where the selected one lost money', async () => {
    api.mockResolvedValue({ as_of: 'now', prices_status: 'ok', strategies: [
      { ...base, strategy_id: 'a', name: 'SPCX 突破', realized_pnl: 54.6, total_pnl: 54.6, trades: 5, win_rate: 40, exit_breakdown: {}, trade_list: [], diagnosis: ['样本太少'] },
      { ...base, strategy_id: 'b', name: 'Donchian', realized_pnl: -312.9, total_pnl: -312.9, trades: 27, win_rate: 25.9,
        exit_breakdown: { 止损: { count: 12, pnl: -381.7 } }, diagnosis: ['亏损主要来自“止损”：12 笔'],
        trade_list: [{ symbol: 'SPY', qty: 20, entry_time: 't1', entry_price: 760, entry_reason: '突破20日高点', exit_time: 't2', exit_price: 745,
          exit_kind: 'stop_loss', exit_label: '止损', exit_reason: '', pnl: -300, pnl_pct: -1.97, holding_minutes: 45, exit_order_id: 'x' }] },
    ] })
    show(<Performance />)
    expect(await screen.findByText('策略排行榜')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Donchian'))
    expect(screen.getByText(/亏损主要来自“止损”/)).toBeInTheDocument()
    expect(screen.getByText('突破20日高点')).toBeInTheDocument()
    expect(screen.getByText('合计 US$-258.30')).toBeInTheDocument()
  })

  it('turns a natural-language request into an unsaved editor draft', async () => {
    const definition = { name: 'RSI 反弹', symbols: ['QQQ'] }
    api.mockImplementation((path: string) => Promise.resolve(path === '/api/ai/settings'
      ? { configured: true, provider: 'openai', base_url: 'http://llm/v1', model: 'm', updated_at: null }
      : { definition, explanation: 'RSI 低于 30 买入', assumptions: ['默认 15 分钟线'], warnings: ['这是草稿：请先回测'] }))
    const onDraft = vi.fn()
    show(<StrategyAssistant current={null} onDraft={onDraft} />)
    fireEvent.change(await screen.findByRole('textbox'), { target: { value: 'QQQ 超卖买入' } })
    fireEvent.click(screen.getByText('生成策略草稿'))
    expect(await screen.findByText(/默认 15 分钟线/)).toBeInTheDocument()
    expect(onDraft).toHaveBeenCalledWith(definition)
    expect(api.mock.calls.map(([path]) => path)).toEqual(['/api/ai/settings', '/api/ai/strategy-draft'])
  })
})
