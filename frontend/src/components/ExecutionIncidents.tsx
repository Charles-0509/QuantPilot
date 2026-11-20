import { formatTime, number } from '../api'
import type { ExecutionIncident } from '../types'
import { Card } from './UI'

export default function ExecutionIncidents({ incidents = [] }: { incidents?: ExecutionIncident[] }) {
  if (!incidents.length) return null
  return <Card style={{ marginTop: 16 }}>
    <div className="card-header"><div><h2>执行安全隔离记录</h2><p>保留触发时的券商快照；两次读取时间分别展示，当前账户可能已变化。同组 bracket/OCO 腿只按最大剩余量预留一次。</p></div></div>
    <div className="card-pad stack">{incidents.map((incident) => {
      const snapshot = incident.details
      return <details key={incident.id}>
        <summary>{incident.symbol} · {incident.status === 'active' ? (incident.details.destructive === false ? '自动核对中' : '隔离中') : incident.details.resolution === 'auto_recovered_consistent' ? '已自动核对通过' : '已控制'} · {formatTime(snapshot.snapshot_completed_at || incident.updated_at)}</summary>
        <p>{incident.reason}</p>
        <p>实际持仓 {snapshot.actual_qty == null ? '未知' : number(snapshot.actual_qty)} 股 · 策略归属 {snapshot.owned_qty == null ? '未知' : number(snapshot.owned_qty)} 股 · 卖单预留 {snapshot.reserved_sell_qty == null ? '未知' : number(snapshot.reserved_sell_qty)} 股</p>
        <p>持仓读取：{formatTime(snapshot.positions_observed_at)} · 订单读取：{formatTime(snapshot.orders_observed_at)}{snapshot.snapshot_attempts ? ` · 核对 ${snapshot.snapshot_attempts} 次` : ''}</p>
        {snapshot.snapshot_error && <p>快照读取失败：{snapshot.snapshot_error}</p>}
        {snapshot.open_orders?.length ? <div className="table-scroll"><table className="data-table">
          <thead><tr><th>卖单号</th><th>父订单／同组</th><th>状态</th><th>委托量</th><th>已成交</th><th>剩余量</th></tr></thead>
          <tbody>{snapshot.open_orders.map((order) => {
            const remaining = order.remaining_qty ?? (order.qty != null && order.filled_qty != null ? Math.max(0, Number(order.qty) - Number(order.filled_qty)) : null)
            return <tr key={order.id}><td>{order.id}</td><td>{order.parent_order_id || '独立订单／组首单'}</td><td>{order.status}</td><td>{order.qty}</td><td>{order.filled_qty}</td><td>{remaining != null && Number.isFinite(remaining) ? number(remaining) : '未知'}</td></tr>
          })}</tbody>
        </table></div> : <p>{snapshot.open_orders ? '当时没有开放卖单' : '旧记录或读取失败，没有开放卖单明细'}</p>}
      </details>
    })}</div>
  </Card>
}
