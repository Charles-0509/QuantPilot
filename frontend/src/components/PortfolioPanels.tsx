import { formatTime, money, number } from '../api'
import type { PortfolioSnapshot } from '../types'
import { Badge, Empty } from './UI'

export function portfolioReady(snapshot?: PortfolioSnapshot) {
  return snapshot?.status === 'ok' && Boolean(snapshot.account)
}

export function SnapshotNotice({ snapshot }: { snapshot?: PortfolioSnapshot }) {
  if (!snapshot?.status) return null
  if (snapshot.status === 'ok') {
    return <p className="field-hint">券商快照 {formatTime(snapshot.as_of)} · 来源 {snapshot.source === 'ibkr' ? 'IBKR' : 'Alpaca'}{snapshot.checks?.length ? ` · ${snapshot.checks.join('；')}` : ''}</p>
  }
  return <div className={snapshot.status === 'suspect' ? 'warning-callout' : 'danger-callout'} style={{ marginBottom: 16 }}>
    <strong>{snapshot.status === 'suspect' ? '账户数据核对未通过，金额暂不显示' : '账户快照暂不可用'}</strong>：{[...(snapshot.errors || []), ...(snapshot.checks || [])].join('；') || '等待券商返回完整数据'}
  </div>
}

export function PositionsTable({ snapshot }: { snapshot?: PortfolioSnapshot }) {
  if (!portfolioReady(snapshot)) return <Empty title="持仓数据暂不可用" detail="系统未取得一致的券商快照，不会把缺失数据显示为零持仓。" />
  const positions = snapshot!.positions || []
  if (!positions.length) return <Empty title="暂时没有持仓" detail="启用策略并启动交易引擎后，新成交形成的持仓会显示在这里。" />
  return <div className="table-scroll" style={{ paddingTop: 12 }}>
    <table className="data-table">
      <thead><tr><th>代码</th><th>数量</th><th>归属</th><th>成本价</th><th>现价</th><th>市值</th><th>未实现盈亏</th><th>今日</th></tr></thead>
      <tbody>{positions.map((position) => <tr key={position.symbol}>
        <td className="symbol-cell">{position.symbol}
          {position.instrument === 'option' && <div className="field-hint">{position.underlying} {position.option_right === 'call' ? '看涨' : '看跌'} {position.strike} · {position.expiration} 到期</div>}
          {position.qty < 0 && <div><Badge tone="warning">空头</Badge></div>}</td>
        <td>{number(position.qty, 4)}{position.instrument === 'option' ? ' 张' : ''}</td>
        <td>{position.owners.map((owner) => `${owner.strategy_name} ${number(owner.qty, 4)}`).join('；') || '—'}
          {position.unattributed_qty > 0 && <div className="field-hint">手动/未归属 {number(position.unattributed_qty, 4)}</div>}</td>
        <td>{money(position.avg_entry_price)}</td>
        <td>{money(position.current_price)}</td>
        <td>{money(position.market_value)}</td>
        <td className={position.unrealized_pl >= 0 ? 'positive' : 'negative'}>{money(position.unrealized_pl)} ({position.unrealized_plpc.toFixed(2)}%)</td>
        <td className={position.change_today_pct >= 0 ? 'positive' : 'negative'}>{position.change_today_pct.toFixed(2)}%</td>
      </tr>)}</tbody>
    </table>
  </div>
}

export function OrdersTable({ snapshot }: { snapshot?: PortfolioSnapshot }) {
  if (!portfolioReady(snapshot)) return <Empty title="开放订单状态未知" detail="等待一致的券商快照。" />
  const orders = snapshot!.orders || []
  if (!orders.length) return <Empty title="没有开放订单" />
  return <div className="table-scroll">
    <table className="data-table">
      <thead><tr><th>代码</th><th>方向</th><th>类型</th><th>数量</th><th>价格</th><th>状态</th><th>策略</th><th>提交时间</th></tr></thead>
      <tbody>{orders.map((order) => <tr key={order.id}>
        <td className="symbol-cell">{order.symbol}</td>
        <td><Badge tone={order.side === 'buy' ? 'success' : 'warning'}>{order.side === 'buy' ? '买入' : '卖出'}</Badge></td>
        <td>{order.type}{order.parent_order_id ? ' · 保护单' : ''}</td>
        <td>{order.qty ?? '—'}{Number(order.filled_qty) > 0 ? ` (已成交 ${order.filled_qty})` : ''}</td>
        <td>{order.limit_price ? `限价 ${money(order.limit_price)}` : order.stop_price ? `止损 ${money(order.stop_price)}` : '市价'}</td>
        <td>{order.status}</td>
        <td>{order.strategy_name || '手动/未归属'}</td>
        <td>{formatTime(order.submitted_at)}</td>
      </tr>)}</tbody>
    </table>
  </div>
}
