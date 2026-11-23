import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Search } from 'lucide-react'
import { api, formatTime } from '../api'
import type { SymbolDetail, Timeframe, WatchlistGroup, WatchQuote } from '../types'
import MarketChart from '../components/market/MarketChart'
import WatchlistPanel from '../components/market/WatchlistPanel'
import SymbolDetailPanel from '../components/market/SymbolDetailPanel'
import { MA_LINES, type Bar } from '../components/market/indicators'
import { useMarketDataInfo } from '../components/MarketDataSourcePanel'
import { Empty, ErrorPanel, Loading } from '../components/UI'
import { readJson, writeJson } from '../storage'
import { formatTradingDate, parseUtcDate } from '../time'

const TIMEFRAMES: Array<[Timeframe, string]> = [['5Min', '5分'], ['15Min', '15分'], ['30Min', '30分'], ['1Hour', '1小时'], ['1Day', '日']]
const STORE = 'qp-market-view'

function loadView() {
  const defaults = { symbol: 'SPY', timeframe: '15Min' as Timeframe, ma: { 20: true, 50: true, 100: true } as Record<number, boolean> }
  return { ...defaults, ...readJson<Partial<typeof defaults>>(STORE, {}) }
}

export default function Market() {
  const client = useQueryClient()
  const initial = useMemo(loadView, [])
  const [symbol, setSymbol] = useState<string>(initial.symbol)
  const [timeframe, setTimeframe] = useState<Timeframe>(initial.timeframe)
  const [visibleMa, setVisibleMa] = useState<Record<number, boolean>>(initial.ma)
  const [search, setSearch] = useState('')
  useEffect(() => { writeJson(STORE, { symbol, timeframe, ma: visibleMa }) }, [symbol, timeframe, visibleMa])

  const marketData = useMarketDataInfo()
  const groups = useQuery({ queryKey: ['watchlist-groups'], queryFn: () => api<WatchlistGroup[]>('/api/watchlist/groups') })
  const allSymbols = useMemo(() => (groups.data || []).flatMap((group) => group.symbols), [groups.data])
  const quoteSymbols = useMemo(() => [...new Set([...allSymbols, symbol])], [allSymbols, symbol])
  const quotes = useQuery({
    queryKey: ['watch-quotes', quoteSymbols.join(',')],
    queryFn: () => api<{ as_of: string; errors?: string[]; quotes: WatchQuote[] }>(`/api/market/watch-quotes?symbols=${encodeURIComponent(quoteSymbols.join(','))}`),
    enabled: quoteSymbols.length > 0,
    refetchInterval: 15000,
    placeholderData: keepPreviousData,
  })
  const quoteMap = useMemo(() => Object.fromEntries((quotes.data?.quotes || []).map((quote) => [quote.symbol, quote])), [quotes.data])
  const detail = useQuery({
    queryKey: ['market-detail', symbol],
    queryFn: () => api<SymbolDetail>(`/api/market/detail?symbol=${encodeURIComponent(symbol)}`),
    refetchInterval: 15000,
    retry: false,
  })
  const bars = useQuery({
    queryKey: ['bars', symbol, timeframe],
    queryFn: () => api<Bar[]>(`/api/market/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}&limit=${timeframe === '1Day' ? 500 : 600}`),
    retry: false,
    refetchInterval: 15000,
    placeholderData: keepPreviousData,
  })
  const addCurrent = useMutation({
    mutationFn: () => api<WatchlistGroup[]>('/api/watchlist/items', { method: 'POST', body: JSON.stringify({ symbol, group_id: groups.data?.[0]?.id }) }),
    onSuccess: (next) => client.setQueryData(['watchlist-groups'], next),
  })

  const last = bars.data?.[bars.data.length - 1]
  const current = quoteMap[symbol] || detail.data
  const showSymbol = (event: FormEvent) => {
    event.preventDefault()
    const next = search.trim().toUpperCase()
    if (/^[A-Z][A-Z0-9.-]{0,14}$/.test(next)) { setSymbol(next); setSearch('') }
  }

  return <div className="market-workspace">
    <section className="market-main" aria-label="K线图">
      <div className="market-toolbar">
        <form className="market-search" onSubmit={showSymbol}>
          <Search size={14} />
          <input aria-label="查看股票" placeholder={symbol} value={search} onChange={(event) => setSearch(event.target.value.toUpperCase())} />
        </form>
        <div className="market-title">
          <h1>{symbol}</h1>
          {current?.price != null && <span className={(current.change ?? 0) >= 0 ? 'positive' : 'negative'}>
            {current.price.toFixed(2)} {current.change_pct != null && `${current.change_pct >= 0 ? '+' : ''}${current.change_pct.toFixed(2)}%`}
          </span>}
        </div>
        <div className="market-segment" role="group" aria-label="K线周期">
          {TIMEFRAMES.map(([value, label]) => <button key={value} className={value === timeframe ? 'active' : ''} aria-pressed={value === timeframe} onClick={() => setTimeframe(value)}>{label}</button>)}
        </div>
        <div className="market-segment" role="group" aria-label="均线">
          {MA_LINES.map(({ period, color }) => <button key={period} className={visibleMa[period] ? 'active' : ''} aria-pressed={visibleMa[period]}
            onClick={() => setVisibleMa((value) => ({ ...value, [period]: !value[period] }))}><i style={{ background: color }} />MA{period}</button>)}
        </div>
        <button className="icon-button" title="刷新" aria-label="刷新行情" onClick={() => { bars.refetch(); quotes.refetch(); detail.refetch() }}><RefreshCw size={15} className={bars.isFetching ? 'spin' : ''} /></button>
      </div>
      <div className="market-chart-area">
        {bars.isLoading ? <Loading label="正在获取K线" /> : bars.error && !bars.data ? <div style={{ padding: 20 }}><ErrorPanel message={(bars.error as Error).message} /></div>
          : bars.data?.length ? <MarketChart bars={bars.data} viewKey={`${symbol}-${timeframe}`} visibleMa={visibleMa} daily={timeframe === '1Day'} /> : <Empty title="没有行情数据" detail="该代码在当前周期没有已完成的 K 线。" />}
      </div>
      <p className="market-footnote">
        数据：{last?.feed === 'sip' ? 'SIP 全市场（15 分钟前）' : last?.feed === 'iex' ? 'IEX 实时（成交量只含 IEX）' : last?.feed || '—'} · {timeframe === '1Day' ? <>最新日线：{last ? formatTradingDate(Math.floor(parseUtcDate(last.timestamp).getTime() / 1000)) : '—'}</> : <>最新已完成K线起始时间：{formatTime(last?.timestamp)}</>} · 最近读取成功：{bars.dataUpdatedAt ? formatTime(new Date(bars.dataUpdatedAt).toISOString()) : '尚未成功'}
        {bars.error ? ' · 刷新失败，显示的数据为缓存' : ' · 每15秒刷新'}{marketData.data ? ` · ${marketData.data.label}` : ''}
      </p>
    </section>
    <aside className="market-side">
      {groups.isLoading ? <Loading label="正在读取自选" /> : groups.error ? <ErrorPanel message={(groups.error as Error).message} />
        : <WatchlistPanel groups={groups.data || []} quotes={quoteMap} selected={symbol} onSelect={setSymbol}
          error={quotes.error ? (quotes.error as Error).message : quotes.data?.errors?.join('；')} />}
      <SymbolDetailPanel detail={detail.data} loading={detail.isLoading} error={detail.error ? (detail.error as Error).message : undefined}
        inWatchlist={allSymbols.includes(symbol)} onAdd={() => addCurrent.mutate()} />
    </aside>
  </div>
}
