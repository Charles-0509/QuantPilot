import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FlaskConical, Play, RotateCcw } from 'lucide-react'
import { api, formatTime, money, number } from '../api'
import type { BacktestRun, Strategy } from '../types'
import EquityChart from '../components/EquityChart'
import { Badge, Button, Card, Empty, ErrorPanel, Field, Loading, PageHeader } from '../components/UI'

function dateOffset(days: number) {
  const date = new Date(); date.setDate(date.getDate() + days); return date.toISOString().slice(0, 10)
}

export default function Backtests() {
  const queryClient = useQueryClient()
  const strategies = useQuery({ queryKey: ['strategies'], queryFn: () => api<Strategy[]>('/api/strategies') })
  const history = useQuery({ queryKey: ['backtests'], queryFn: () => api<BacktestRun[]>('/api/backtests') })
  const available = useMemo(() => strategies.data || [], [strategies.data])
  const [strategyId, setStrategyId] = useState('')
  const [start, setStart] = useState(dateOffset(-365))
  const [end, setEnd] = useState(dateOffset(-1))
  const [capital, setCapital] = useState(100000)
  const [slippage, setSlippage] = useState(5)
  const selected = strategyId || available[0]?.id || ''
  const run = useMutation({
    mutationFn: () => api<BacktestRun>('/api/backtests', { method: 'POST', body: JSON.stringify({ strategy_id: selected, start: new Date(`${start}T00:00:00Z`).toISOString(), end: new Date(`${end}T23:59:59Z`).toISOString(), initial_cash: capital, slippage_bps: slippage, commission: 0, benchmark: 'SPY' }) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['backtests'] }),
  })
  const result = run.data || history.data?.[0]
  const metrics = result?.metrics || {}
  if (strategies.isLoading) return <Loading label="正在载入策略" />

  return <>
    <PageHeader eyebrow="BACKTEST LAB / NO LOOK-AHEAD" title="回测实验室" description="信号在K线完成后生成，市价单在下一根K线开盘成交；同一根K线同时触及止损和止盈时优先按止损处理。" actions={<Badge tone="info">默认滑点 5 bps</Badge>} />
    <Card style={{ marginBottom: 16 } as any}>
      <div className="form-section">
        <div className="form-section-title"><div><h3>回测任务</h3><p>历史行情来自 Alpaca IEX，默认使用复权K线。</p></div><FlaskConical size={19} color="#3df6de" /></div>
        <div className="form-grid-3">
          <Field label="选择策略"><select value={selected} onChange={(e) => setStrategyId(e.target.value)}>{available.map((strategy) => <option value={strategy.id} key={strategy.id}>{strategy.name}{strategy.is_template ? '（模板）' : ''}</option>)}</select></Field>
          <Field label="开始日期"><input type="date" value={start} onChange={(e) => setStart(e.target.value)} /></Field>
          <Field label="结束日期"><input type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></Field>
          <Field label="初始资金"><input type="number" value={capital} onChange={(e) => setCapital(Number(e.target.value))} /></Field>
          <Field label="滑点（基点）"><input type="number" value={slippage} onChange={(e) => setSlippage(Number(e.target.value))} /></Field>
          <div style={{ display: 'flex', alignItems: 'end' }}><Button disabled={!selected || run.isPending} onClick={() => run.mutate()}>{run.isPending ? <RotateCcw className="spin" size={15} /> : <Play size={15} />}{run.isPending ? '正在计算' : '运行回测'}</Button></div>
        </div>
        {run.error && <ErrorPanel message={(run.error as Error).message} />}
      </div>
    </Card>
    {result ? <div className="stack">
      {result.error && <ErrorPanel message={result.error} />}
      <div className="stat-grid">
        <Metric label="总收益" value={`${number(metrics.total_return_pct)}%`} positive={Number(metrics.total_return_pct) >= 0} />
        <Metric label="最大回撤" value={`${number(metrics.max_drawdown_pct)}%`} />
        <Metric label="Sharpe" value={number(metrics.sharpe, 3)} />
        <Metric label="胜率" value={`${number(metrics.win_rate_pct)}%`} />
      </div>
      <Card><div className="card-header"><div><h2>策略净值与 SPY 基准</h2><p>运行时间：{formatTime(result.created_at)}</p></div><Badge tone={result.status === 'completed' ? 'success' : result.status === 'failed' ? 'danger' : 'warning'}>{result.status}</Badge></div><div className="card-pad"><EquityChart equity={result.equity_curve} benchmark={result.benchmark_curve} /></div></Card>
      <Card><div className="card-header"><div><h2>完整绩效指标</h2><p>指标仅代表历史模拟，不保证未来表现</p></div></div><div className="metric-grid">{Object.entries(metrics).map(([key, value]) => <div className="metric-item" key={key}><span>{metricName(key)}</span><strong>{number(value, 3)}</strong></div>)}</div></Card>
      <Card><div className="card-header"><div><h2>交易明细</h2><p>保守处理滑点和同K线止盈止损顺序</p></div><Badge>{result.trades.length} 笔</Badge></div>
        {result.trades.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>代码</th><th>入场</th><th>离场</th><th>数量</th><th>买入价</th><th>卖出价</th><th>盈亏</th><th>原因</th></tr></thead><tbody>{result.trades.map((trade, index) => <tr key={index}><td className="symbol-cell">{trade.symbol}</td><td>{formatTime(String(trade.entry_time))}</td><td>{formatTime(String(trade.exit_time))}</td><td>{number(trade.qty, 4)}</td><td>{money(trade.entry_price)}</td><td>{money(trade.exit_price)}</td><td className={Number(trade.pnl) >= 0 ? 'positive' : 'negative'}>{money(trade.pnl)}</td><td>{trade.reason}</td></tr>)}</tbody></table></div> : <Empty title="该区间没有产生交易" />}
      </Card>
    </div> : <Card><Empty title="还没有回测结果" detail="选择策略与日期区间，然后运行第一次无未来数据泄漏回测。" /></Card>}
  </>
}

function Metric({ label, value, positive }: { label: string; value: string; positive?: boolean }) {
  return <Card className="stat-card"><div className="stat-topline"><span>{label}</span></div><div className={`stat-value ${positive === undefined ? '' : positive ? 'positive' : 'negative'}`}>{value}</div></Card>
}
function metricName(value: string) {
  return ({ initial_cash: '初始资金', final_equity: '最终净值', total_return_pct: '总收益 %', cagr_pct: '年化收益 %', annual_volatility_pct: '年化波动 %', sharpe: 'Sharpe', sortino: 'Sortino', max_drawdown_pct: '最大回撤 %', win_rate_pct: '胜率 %', payoff_ratio: '盈亏比', profit_factor: 'Profit Factor', trade_count: '交易次数', average_bars_held: '平均持有K线', exposure_pct: '资金利用率 %' } as Record<string,string>)[value] || value
}
