import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, RefreshCw, X } from 'lucide-react'
import { api } from '../api'
import type { Timeframe } from '../types'
import CandleChart from '../components/CandleChart'
import { Button, Card, Empty, ErrorPanel, Field, Loading, PageHeader } from '../components/UI'

export default function Market() {
  const queryClient = useQueryClient()
  const watchlist = useQuery({ queryKey: ['watchlist'], queryFn: () => api<string[]>('/api/watchlist') })
  const [symbol, setSymbol] = useState('SPY')
  const [timeframe, setTimeframe] = useState<Timeframe>('15Min')
  const [newSymbol, setNewSymbol] = useState('')
  const bars = useQuery({
    queryKey: ['bars', symbol, timeframe],
    queryFn: () => api<any[]>(`/api/market/bars?symbol=${symbol}&timeframe=${timeframe}&limit=260`),
    retry: false,
  })
  const saveWatchlist = useMutation({
    mutationFn: (symbols: string[]) => api<string[]>('/api/watchlist', { method: 'PUT', body: JSON.stringify({ symbols }) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['watchlist'] }),
  })
  const symbols = watchlist.data || []
  const last = useMemo(() => bars.data?.[bars.data.length - 1], [bars.data])

  return <>
    <PageHeader eyebrow="MARKET INTELLIGENCE" title="行情中心" description="使用 Alpaca 免费 IEX 行情查看已完成K线。图表只用于模拟盘策略研究，不包含付费 SIP 全市场数据。" actions={
      <Button variant="ghost" onClick={() => bars.refetch()}><RefreshCw size={15} />刷新行情</Button>
    } />
    <div className="dashboard-grid">
      <Card>
        <div className="chart-toolbar">
          <Field label="股票代码"><select value={symbol} onChange={(e) => setSymbol(e.target.value)}>{symbols.map((item) => <option key={item}>{item}</option>)}</select></Field>
          <Field label="K线周期"><select value={timeframe} onChange={(e) => setTimeframe(e.target.value as Timeframe)}>
            <option value="5Min">5 分钟</option><option value="15Min">15 分钟</option><option value="30Min">30 分钟</option><option value="1Hour">1 小时</option><option value="1Day">日线</option>
          </select></Field>
          {last && <div style={{ marginLeft: 'auto' }}><span className="eyebrow">LATEST CLOSE</span><div style={{ fontSize: 25, fontWeight: 700, marginTop: 5 }}>${Number(last.close).toFixed(2)}</div></div>}
        </div>
        {bars.isLoading ? <Loading label="正在获取 IEX K线" /> : bars.error ? <div style={{ padding: 20 }}><ErrorPanel message={(bars.error as Error).message} /></div> : bars.data?.length ? <CandleChart bars={bars.data} /> : <Empty title="没有行情数据" />}
      </Card>
      <Card>
        <div className="card-header"><div><h2>自选股票池</h2><p>实时订阅与策略合计最多30个代码</p></div></div>
        <div className="card-pad">
          <div style={{ display: 'flex', gap: 8 }}>
            <input placeholder="例如 AAPL" value={newSymbol} onChange={(e) => setNewSymbol(e.target.value.toUpperCase())} />
            <Button onClick={() => { const next = [...new Set([...symbols, newSymbol.trim().toUpperCase()])].filter(Boolean); if (next.length <= 30) saveWatchlist.mutate(next); setNewSymbol('') }} disabled={!newSymbol.trim()}><Plus size={15} />添加</Button>
          </div>
          <div className="strategy-meta" style={{ marginTop: 18 }}>{symbols.map((item) => <button className={`badge ${item === symbol ? 'badge-info' : 'badge-neutral'}`} key={item} onClick={() => setSymbol(item)}>
            {item}<X size={12} onClick={(event) => { event.stopPropagation(); saveWatchlist.mutate(symbols.filter((value) => value !== item)) }} />
          </button>)}</div>
          <div className="warning-callout" style={{ marginTop: 20 }}>免费 IEX 只代表 IEX 交易所的实时成交。依赖全市场成交量、盘口或极短周期的策略可能与 SIP 数据产生差异。</div>
        </div>
      </Card>
    </div>
  </>
}
