import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, formatTime, money } from '../api'
import EquityChart from '../components/EquityChart'
import { Badge, Card, Empty, ErrorPanel, Loading, PageHeader } from '../components/UI'
import type { StrategyPerformance } from '../types'

function pnlClass(value: number | null | undefined) {
  return (value ?? 0) >= 0 ? 'positive' : 'negative'
}

function minutes(value: number | null) {
  if (value == null) return '—'
  return value >= 1440 ? `${(value / 1440).toFixed(1)} 天` : value >= 60 ? `${(value / 60).toFixed(1)} 小时` : `${value.toFixed(0)} 分钟`
}

export default function Performance() {
  const query = useQuery({
    queryKey: ['performance'],
    queryFn: () => api<{ as_of: string; prices_status: string; strategies: StrategyPerformance[] }>('/api/performance/strategies'),
    refetchInterval: 60000,
  })
  const [selected, setSelected] = useState<string | null>(null)
  if (query.isLoading) return <Loading label="正在按策略汇总模拟盘成交" />
  if (query.error) return <ErrorPanel message={(query.error as Error).message} />
  const rows = query.data!.strategies
  const current = rows.find((row) => row.strategy_id === selected) || rows[0]
  const total = rows.reduce((sum, row) => sum + row.total_pnl, 0)

  return <>
    <PageHeader eyebrow="LIVE PAPER ATTRIBUTION" title="策略绩效" description="用每个策略自己的模拟盘成交计算盈亏，不含手动持仓、现金管理和其他策略。数据未扣手续费。" />
    {query.data!.prices_status !== 'ok' && <div className="warning-callout" style={{ marginBottom: 16 }}>当前持仓价格暂不可用，未实现盈亏可能缺失；已平仓盈亏不受影响。</div>}
    <Card>
      <div className="card-header"><div><h2>策略排行榜</h2><p>按总盈亏排序；点击一行查看明细 · 更新于 {formatTime(query.data!.as_of)}</p></div>
        <Badge tone={total >= 0 ? 'success' : 'danger'}>合计 {money(total)}</Badge></div>
      {rows.length ? <div className="table-scroll"><table className="data-table">
        <thead><tr><th>策略</th><th>总盈亏</th><th>已实现</th><th>未实现</th><th>交易</th><th>胜率</th><th>盈亏比</th><th>最大回撤</th><th>状态</th></tr></thead>
        <tbody>{rows.map((row) => <tr key={row.strategy_id} onClick={() => setSelected(row.strategy_id)} style={{ cursor: 'pointer', background: row.strategy_id === current?.strategy_id ? 'rgba(61,246,222,.06)' : undefined }}>
          <td><strong>{row.name}</strong><div className="field-hint">{row.symbols.join(' · ')}</div></td>
          <td className={pnlClass(row.total_pnl)}>{money(row.total_pnl)}</td>
          <td className={pnlClass(row.realized_pnl)}>{money(row.realized_pnl)}</td>
          <td className={pnlClass(row.unrealized_pnl)}>{row.open_lots.length ? money(row.unrealized_pnl) : '—'}</td>
          <td>{row.trades}</td>
          <td>{row.win_rate == null ? '—' : `${row.win_rate.toFixed(0)}%`}</td>
          <td>{row.profit_factor == null ? (row.trades && !row.losses ? '无亏损' : '—') : row.profit_factor.toFixed(2)}</td>
          <td className="negative">{row.trades ? money(row.max_drawdown) : '—'}</td>
          <td><Badge tone={row.enabled ? 'success' : 'neutral'}>{row.enabled ? '已启用' : '已停用'}</Badge></td>
        </tr>)}</tbody></table></div> : <Empty title="还没有策略成交" detail="策略在模拟盘成交后，这里会自动出现。" />}
    </Card>
    {current && <StrategyDetail row={current} />}
  </>
}

function StrategyDetail({ row }: { row: StrategyPerformance }) {
  return <>
    <div className="two-column" style={{ marginTop: 16 }}>
      <Card>
        <div className="card-header"><div><h2>{row.name}：为什么赚 / 亏</h2><p>只根据本策略成交得出的观察</p></div>
          <Link className="button button-ghost" to={`/strategies/${row.strategy_id}`}>编辑策略</Link></div>
        <div className="card-pad stack">
          {row.diagnosis.map((note) => <p key={note}>• {note}</p>)}
          {row.ownership_cleared_qty > 0 && <p className="field-hint">另有 {row.ownership_cleared_qty} 股在安全隔离时被清除策略归属，没有对应卖出成交，未计入盈亏。</p>}
          <div className="table-scroll"><table className="data-table"><thead><tr><th>离场方式</th><th>次数</th><th>合计盈亏</th></tr></thead>
            <tbody>{Object.entries(row.exit_breakdown).map(([label, item]) => <tr key={label}><td>{label}</td><td>{item.count}</td><td className={pnlClass(item.pnl)}>{money(item.pnl)}</td></tr>)}</tbody></table></div>
          <p className="field-hint">平均盈利 {row.avg_win == null ? '—' : money(row.avg_win)} · 平均亏损 {row.avg_loss == null ? '—' : money(row.avg_loss)} · 平均持仓 {minutes(row.avg_holding_minutes)}</p>
        </div>
      </Card>
      <Card>
        <div className="card-header"><div><h2>累计已实现盈亏</h2><p>每笔平仓后的累计结果</p></div></div>
        {row.curve.length ? <EquityChart equity={row.curve.map((point) => ({ timestamp: point.time, equity: point.pnl }))} /> : <Empty title="尚无已平仓交易" />}
      </Card>
    </div>
    <Card style={{ marginTop: 16 } as any}>
      <div className="card-header"><div><h2>逐笔交易</h2><p>入场理由来自买入信号，离场方式来自成交订单类型</p></div></div>
      {row.open_lots.length > 0 && <div className="warning-callout" style={{ margin: '0 16px 12px' }}>未平仓：{row.open_lots.map((lot) => `${lot.symbol} ${lot.qty} 股 @ ${money(lot.entry_price)}`).join('；')}</div>}
      {row.trade_list.length ? <div className="table-scroll"><table className="data-table">
        <thead><tr><th>代码</th><th>入场</th><th>离场</th><th>数量</th><th>盈亏</th><th>持仓</th><th>离场方式</th><th>入场理由</th></tr></thead>
        <tbody>{row.trade_list.map((trade) => <tr key={`${trade.exit_order_id}-${trade.entry_time}`}>
          <td className="symbol-cell">{trade.symbol}</td>
          <td>{formatTime(trade.entry_time)}<div className="field-hint">{money(trade.entry_price)}</div></td>
          <td>{formatTime(trade.exit_time)}<div className="field-hint">{money(trade.exit_price)}</div></td>
          <td>{trade.qty}</td>
          <td className={pnlClass(trade.pnl)}>{money(trade.pnl)}<div className="field-hint">{trade.pnl_pct.toFixed(2)}%</div></td>
          <td>{minutes(trade.holding_minutes)}</td>
          <td>{trade.exit_label}{trade.exit_reason ? `：${trade.exit_reason}` : ''}</td>
          <td>{trade.entry_reason || '—'}</td>
        </tr>)}</tbody></table></div> : <Empty title="尚无已平仓交易" />}
    </Card>
  </>
}
