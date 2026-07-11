import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, Clock3, FlaskConical, History, Play, RotateCcw, Target } from 'lucide-react'
import { api, ApiError, formatTime, money, number } from '../api'
import type { BacktestRun, BacktestSummary, Strategy } from '../types'
import EquityChart from '../components/EquityChart'
import { Badge, Button, Card, Empty, ErrorPanel, Field, Loading, PageHeader } from '../components/UI'
import { shanghaiDateOffset } from '../time'

const SYMBOL_PRESETS = ['GOOGL', 'AAPL', 'MSFT', 'NVDA', 'AMZN', 'META', 'TSLA', 'SPY', 'QQQ', 'IWM']
const BENCHMARKS = [
  ['SPY', '标普500'],
  ['QQQ', '纳斯达克100'],
  ['IWM', '罗素2000小盘股'],
  ['DIA', '道琼斯工业指数'],
  ['VTI', '美国全市场'],
  ['VOO', '标普500低费率'],
  ['RSP', '标普500等权重'],
  ['XLK', '美国科技板块'],
] as const
const TRADE_PAGE_SIZE = 50

function parseSymbols(value: string) {
  return [...new Set(value.split(/[,，\s]+/).map((item) => item.trim().toUpperCase()).filter(Boolean))]
}

function parameterSymbols(parameters?: Record<string, unknown>) {
  const value = parameters?.symbols
  return Array.isArray(value) ? value.map(String) : []
}

function parameterBenchmark(parameters?: Record<string, unknown>) {
  return String(parameters?.benchmark || 'SPY').toUpperCase()
}

export default function Backtests() {
  const queryClient = useQueryClient()
  const strategies = useQuery({
    queryKey: ['strategies'],
    queryFn: () => api<Strategy[]>('/api/strategies'),
    staleTime: 30_000,
  })
  const history = useQuery({
    queryKey: ['backtests'],
    queryFn: () => api<BacktestSummary[]>('/api/backtests'),
    refetchInterval: (query) => query.state.data?.some((item) => ['queued', 'running'].includes(item.status)) ? 1500 : false,
  })
  const available = useMemo(() => strategies.data || [], [strategies.data])
  const [strategyId, setStrategyId] = useState('')
  const [symbolInput, setSymbolInput] = useState('')
  const [benchmark, setBenchmark] = useState('SPY')
  const [start, setStart] = useState(shanghaiDateOffset(-365))
  const [end, setEnd] = useState(shanghaiDateOffset(-1))
  const [capital, setCapital] = useState(100000)
  const [slippage, setSlippage] = useState(5)
  const [activeRunId, setActiveRunId] = useState('')
  const [tradePage, setTradePage] = useState(0)

  useEffect(() => {
    if (!strategyId && available[0]) setStrategyId(available[0].id)
  }, [available, strategyId])

  const selectedStrategy = available.find((strategy) => strategy.id === strategyId)
  useEffect(() => {
    if (selectedStrategy) setSymbolInput(selectedStrategy.definition.symbols.join(', '))
  }, [selectedStrategy?.id])

  useEffect(() => {
    if (!activeRunId && history.data?.[0]) setActiveRunId(history.data[0].id)
  }, [activeRunId, history.data])

  const detail = useQuery({
    queryKey: ['backtest', activeRunId],
    queryFn: () => api<BacktestRun>(`/api/backtests/${activeRunId}`),
    enabled: Boolean(activeRunId),
    refetchInterval: (query) => ['queued', 'running'].includes(query.state.data?.status || '') ? 1000 : false,
  })

  const symbols = useMemo(() => parseSymbols(symbolInput), [symbolInput])
  const symbolError = symbols.length === 0
    ? '至少填写一个回测标的'
    : symbols.length > 10
      ? '单次回测最多支持10个标的'
      : symbols.some((symbol) => !/^[A-Z][A-Z0-9.-]{0,14}$/.test(symbol))
        ? '股票代码格式不正确'
        : ''

  const run = useMutation({
    mutationFn: () => api<BacktestSummary>('/api/backtests', {
      method: 'POST',
      body: JSON.stringify({
        strategy_id: strategyId,
        symbols,
        start: new Date(`${start}T00:00:00Z`).toISOString(),
        end: new Date(`${end}T23:59:59Z`).toISOString(),
        initial_cash: capital,
        slippage_bps: slippage,
        commission: 0,
        benchmark,
      }),
    }),
    onSuccess: (created) => {
      setActiveRunId(created.id)
      setTradePage(0)
      queryClient.setQueryData<BacktestRun>(['backtest', created.id], {
        ...created,
        equity_curve: [],
        benchmark_curve: [],
        trades: [],
      })
      queryClient.invalidateQueries({ queryKey: ['backtests'] })
    },
  })

  const result = detail.data
  useEffect(() => {
    setTradePage(0)
    if (result && ['completed', 'failed'].includes(result.status)) {
      queryClient.invalidateQueries({ queryKey: ['backtests'] })
    }
  }, [queryClient, result?.id, result?.status])

  const metrics = result?.metrics || {}
  const running = Boolean(result && ['queued', 'running'].includes(result.status))
  const trades = result?.trades || []
  const tradePageCount = Math.max(1, Math.ceil(trades.length / TRADE_PAGE_SIZE))
  const visibleTrades = trades.slice(tradePage * TRADE_PAGE_SIZE, (tradePage + 1) * TRADE_PAGE_SIZE)
  const resultBenchmark = parameterBenchmark(result?.parameters)
  const resultStrategy = result ? available.find((strategy) => strategy.id === result.strategy_id) : undefined
  const savedResultSymbols = parameterSymbols(result?.parameters)
  const resultSymbols = savedResultSymbols.length ? savedResultSymbols : resultStrategy?.definition.symbols || []
  const strategyNames = useMemo(() => new Map(available.map((item) => [item.id, item.name])), [available])

  if (strategies.isLoading) return <Loading label="正在载入策略" />

  const submit = () => {
    if (!strategyId || symbolError || !start || !end || start >= end || capital <= 0 || slippage < 0) return
    run.mutate()
  }

  return <>
    <PageHeader
      eyebrow="BACKTEST LAB / ASYNC ENGINE"
      title="回测实验室"
      description="把同一套策略规则临时应用到不同股票或ETF；任务在后台计算，离开页面也不会阻塞交易控制台。"
      actions={<><Badge tone="info">曲线智能降采样</Badge><Badge tone="neutral">默认滑点 5 bps</Badge></>}
    />

    <Card style={{ marginBottom: 16 } as any}>
      <div className="form-section">
        <div className="form-section-title"><div><h3>配置回测任务</h3><p>历史行情来自 Alpaca IEX；覆盖标的只影响本次回测，不会修改原策略。</p></div><FlaskConical size={19} color="#3df6de" /></div>
        <div className="backtest-form-grid">
          <Field label="选择策略" hint="使用该策略的指标、入场、离场、仓位和风控规则。">
            <select aria-label="选择策略" value={strategyId} onChange={(event) => setStrategyId(event.target.value)}>
              {available.map((strategy) => <option value={strategy.id} key={strategy.id}>{strategy.name}{strategy.is_template ? '（模板）' : ''}</option>)}
            </select>
          </Field>
          <Field label="回测标的" hint="可输入一个或多个代码，用逗号分隔，最多10个。">
            <input aria-label="回测标的" value={symbolInput} onChange={(event) => setSymbolInput(event.target.value.toUpperCase())} placeholder="例如 GOOGL 或 AAPL, MSFT" />
          </Field>
          <Field label="对比基准" hint="衡量策略是否优于代表性市场ETF。">
            <select aria-label="对比基准" value={benchmark} onChange={(event) => setBenchmark(event.target.value)}>
              {BENCHMARKS.map(([symbol, label]) => <option key={symbol} value={symbol}>{symbol} · {label}</option>)}
            </select>
          </Field>
          <Field label="开始日期"><input aria-label="开始日期" type="date" value={start} onChange={(event) => setStart(event.target.value)} /></Field>
          <Field label="结束日期"><input aria-label="结束日期" type="date" value={end} onChange={(event) => setEnd(event.target.value)} /></Field>
          <Field label="初始资金"><input aria-label="初始资金" type="number" min="1" value={capital} onChange={(event) => setCapital(Number(event.target.value))} /></Field>
          <Field label="滑点（基点）"><input aria-label="滑点（基点）" type="number" min="0" max="1000" value={slippage} onChange={(event) => setSlippage(Number(event.target.value))} /></Field>
          <div className="backtest-submit"><Button disabled={!strategyId || Boolean(symbolError) || !start || !end || start >= end || capital <= 0 || slippage < 0 || run.isPending} onClick={submit}>{run.isPending ? <RotateCcw className="spin" size={15} /> : <Play size={15} />}{run.isPending ? '正在提交' : '运行回测'}</Button></div>
        </div>
        <div className="symbol-presets" aria-label="常用回测标的">
          <span>快速选择</span>
          {SYMBOL_PRESETS.map((symbol) => <button type="button" key={symbol} className={symbols.length === 1 && symbols[0] === symbol ? 'active' : ''} onClick={() => setSymbolInput(symbol)}>{symbol}</button>)}
          {selectedStrategy && <button type="button" onClick={() => setSymbolInput(selectedStrategy.definition.symbols.join(', '))}>恢复策略股票池</button>}
        </div>
        {symbolError && <div className="form-error" role="alert">{symbolError}</div>}
        {start >= end && <div className="form-error" role="alert">结束日期必须晚于开始日期</div>}
        {run.error && <ErrorPanel message={run.error instanceof ApiError ? run.error.message : (run.error as Error).message} />}
      </div>
    </Card>

    {running && <Card className="backtest-progress" style={{ marginBottom: 16 } as any}>
      <div className="reactor"><RotateCcw className="spin" size={24} /></div>
      <div><p className="eyebrow">BACKGROUND COMPUTE</p><h2>{result?.status === 'queued' ? '回测已进入队列' : '正在计算历史交易'}</h2><p>{resultSymbols.join('、')} · 对比 {resultBenchmark}。页面仍可正常操作，结果完成后会自动刷新。</p></div>
    </Card>}

    {result && !running ? <div className="stack">
      {result.error && <ErrorPanel message={result.error} />}
      {result.status === 'completed' && <>
        <div className="stat-grid">
          <Metric label="总收益" value={`${number(metrics.total_return_pct)}%`} positive={Number(metrics.total_return_pct) >= 0} />
          <Metric label="最大回撤" value={`${number(metrics.max_drawdown_pct)}%`} />
          <Metric label="Sharpe" value={number(metrics.sharpe, 3)} />
          <Metric label="胜率" value={`${number(metrics.win_rate_pct)}%`} />
        </div>
        <Card><div className="card-header"><div><h2>策略净值与 {resultBenchmark} 基准</h2><p>{resultSymbols.join('、')} · 运行时间：{formatTime(result.created_at)}</p></div><Badge tone="success">已完成</Badge></div><div className="card-pad"><EquityChart equity={result.equity_curve} benchmark={result.benchmark_curve} benchmarkLabel={resultBenchmark} /></div></Card>
        <Card><div className="card-header"><div><h2>完整绩效指标</h2><p>指标仅代表历史模拟，不保证未来表现</p></div></div><div className="metric-grid">{Object.entries(metrics).map(([key, value]) => <div className="metric-item" key={key}><span>{metricName(key)}</span><strong>{number(value, 3)}</strong></div>)}</div></Card>
        <Card><div className="card-header"><div><h2>交易明细</h2><p>每页最多显示50笔，避免大量交易拖慢浏览器</p></div><Badge>{trades.length} 笔</Badge></div>
          {trades.length ? <><div className="table-scroll"><table className="data-table"><thead><tr><th>代码</th><th>入场</th><th>离场</th><th>数量</th><th>买入价</th><th>卖出价</th><th>盈亏</th><th>原因</th></tr></thead><tbody>{visibleTrades.map((trade, index) => <tr key={`${trade.symbol}-${trade.entry_time}-${index}`}><td className="symbol-cell">{trade.symbol}</td><td>{formatTime(String(trade.entry_time))}</td><td>{formatTime(String(trade.exit_time))}</td><td>{number(trade.qty, 4)}</td><td>{money(trade.entry_price)}</td><td>{money(trade.exit_price)}</td><td className={Number(trade.pnl) >= 0 ? 'positive' : 'negative'}>{money(trade.pnl)}</td><td>{trade.reason}</td></tr>)}</tbody></table></div>
            {tradePageCount > 1 && <div className="table-pagination"><Button variant="ghost" disabled={tradePage === 0} onClick={() => setTradePage((page) => page - 1)}>上一页</Button><span>第 {tradePage + 1} / {tradePageCount} 页</span><Button variant="ghost" disabled={tradePage + 1 >= tradePageCount} onClick={() => setTradePage((page) => page + 1)}>下一页</Button></div>}
          </> : <Empty title="该区间没有产生交易" />}
        </Card>
      </>}
    </div> : !running && !detail.isLoading && <Card><Empty title="还没有回测结果" detail="选择策略、回测标的与日期区间，然后运行第一次回测。" /></Card>}

    <BacktestHistory items={history.data || []} activeId={activeRunId} strategyNames={strategyNames} onSelect={(id) => { setActiveRunId(id); setTradePage(0) }} />
  </>
}

function BacktestHistory({ items, activeId, strategyNames, onSelect }: { items: BacktestSummary[]; activeId: string; strategyNames: Map<string, string>; onSelect: (id: string) => void }) {
  if (!items.length) return null
  return <Card style={{ marginTop: 16 } as any}>
    <div className="card-header"><div><h2>最近回测</h2><p>列表只加载摘要，点击时才读取完整曲线和交易明细</p></div><History size={18} color="#a775ff" /></div>
    <div className="backtest-history-list">
      {items.slice(0, 12).map((item) => {
        const symbols = parameterSymbols(item.parameters)
        const benchmark = parameterBenchmark(item.parameters)
        const active = item.id === activeId
        return <button type="button" className={`backtest-history-item ${active ? 'active' : ''}`} key={item.id} onClick={() => onSelect(item.id)}>
          <span className={`history-status ${item.status}`}>{['queued', 'running'].includes(item.status) ? <RotateCcw className="spin" size={14} /> : item.status === 'completed' ? <CheckCircle2 size={14} /> : <Clock3 size={14} />}</span>
          <span><strong>{strategyNames.get(item.strategy_id) || '历史策略'}</strong><small>{symbols.join('、') || '原策略股票池'} · 基准 {benchmark} · {formatTime(item.created_at)}</small></span>
          <span className={Number(item.metrics?.total_return_pct) >= 0 ? 'positive' : 'negative'}>{item.status === 'completed' ? `${number(item.metrics.total_return_pct)}%` : statusName(item.status)}</span>
        </button>
      })}
    </div>
  </Card>
}

function Metric({ label, value, positive }: { label: string; value: string; positive?: boolean }) {
  return <Card className="stat-card"><div className="stat-topline"><span>{label}</span><Target size={15} /></div><div className={`stat-value ${positive === undefined ? '' : positive ? 'positive' : 'negative'}`}>{value}</div></Card>
}

function statusName(value: string) {
  return ({ queued: '排队中', running: '计算中', failed: '失败', completed: '已完成' } as Record<string, string>)[value] || value
}

function metricName(value: string) {
  return ({ initial_cash: '初始资金', final_equity: '最终净值', total_return_pct: '总收益 %', cagr_pct: '年化收益 %', annual_volatility_pct: '年化波动 %', sharpe: 'Sharpe', sortino: 'Sortino', max_drawdown_pct: '最大回撤 %', win_rate_pct: '胜率 %', payoff_ratio: '盈亏比', profit_factor: 'Profit Factor', trade_count: '交易次数', average_bars_held: '平均持有K线', exposure_pct: '资金利用率 %' } as Record<string, string>)[value] || value
}
