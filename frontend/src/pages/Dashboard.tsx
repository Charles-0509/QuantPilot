import { useQuery } from '@tanstack/react-query'
import { Activity, CircleDollarSign, Landmark, Power, Radio, WalletCards } from 'lucide-react'
import { api, formatTime, money } from '../api'
import { actionLabel, actionTone } from '../labels'
import { connectionPresentation, enginePresentation } from '../status'
import type { DashboardData } from '../types'
import { Badge, Card, Empty, ErrorPanel, Loading, PageHeader, StatCard } from '../components/UI'
import { OrdersTable, PositionsTable, SnapshotNotice, portfolioReady } from '../components/PortfolioPanels'
import ExecutionIncidents from '../components/ExecutionIncidents'

export default function Dashboard() {
  const query = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => api<DashboardData>('/api/dashboard'),
    refetchInterval: 10000,
  })
  if (query.isLoading) return <Loading label="正在同步券商账户" />
  if (query.error) return <ErrorPanel message={(query.error as Error).message} />
  const data = query.data!
  const portfolio = data.portfolio
  const ready = portfolioReady(portfolio)
  const account = ready ? portfolio!.account! : null
  const connectionView = connectionPresentation(data.connection)
  const engineView = enginePresentation(data.engine)
  const clockKnown = typeof portfolio?.market_open === 'boolean'
  const marketOpen = Boolean(portfolio?.market_open)
  const dayChange = account?.day_change_pct ?? undefined
  const connectionDetail = connectionView.state === 'unconfigured'
    ? '应用和策略模板已经可以浏览；配置券商模拟账户后即可获取行情、回测并自动交易。'
    : data.connection.retry_at
      ? `系统会自动恢复连接，下次重试：${formatTime(data.connection.retry_at)}。`
      : '系统会在后台自动重试，恢复后无需重新启动交易引擎。'
  const engineReason = engineView.operationalStatus === 'paused'
    ? data.engine.reason
    : data.engine.operational_reason || data.engine.reason

  return (
    <>
      <PageHeader
        eyebrow={data.connection.paper === false ? 'MISSION CONTROL / LIVE ACCOUNT' : 'MISSION CONTROL / PAPER ENVIRONMENT'}
        title="量化交易控制台"
        description="集中查看当前券商账户、策略引擎、持仓、信号与系统健康状态。交易环境以当前账户标识为准。"
        actions={<Badge tone={marketOpen ? 'success' : 'neutral'}>{clockKnown ? (marketOpen ? '美股交易中' : '美股已休市') : '市场状态未知'}</Badge>}
      />
      {data.connection.paper === false && <div className="danger-callout" style={{ marginBottom: 16 }}>当前连接 IBKR 真实账户，策略下单将影响真实资金。</div>}
      {connectionView.state !== 'connected' && (
        <div className={connectionView.tone === 'danger' ? 'danger-callout' : 'warning-callout'} style={{ marginBottom: 16 }}>
          <strong>{connectionView.label}</strong>：{data.connection.message || '暂时无法确认券商连接状态'}。{connectionDetail}
        </div>
      )}
      <SnapshotNotice snapshot={portfolio} />
      <div className="stat-grid">
        <StatCard label="账户净值" value={account ? money(account.equity) : '—'} trend={dayChange} detail={account ? '相对上一交易日收盘净值' : '等待一致的券商快照'} icon={<Landmark size={18} />} />
        <StatCard label="可用购买力" value={account ? money(account.buying_power) : '—'} detail={account ? `现金 ${money(account.cash)}` : '账户数据暂不可用'} icon={<CircleDollarSign size={18} />} />
        <StatCard label="当前持仓" value={ready ? `${portfolio!.positions?.length || 0} 只` : '—'} detail={ready ? `未成交订单 ${portfolio!.orders?.length || 0} 笔 · 仓位 ${(account?.exposure_pct ?? 0).toFixed(1)}%` : '开放订单状态未知'} icon={<WalletCards size={18} />} />
        <StatCard label="交易引擎" value={engineView.label} detail={`${data.engine.enabled_strategies ?? 0} 个策略已启用 · ${engineReason || '等待操作'}`} icon={<Power size={18} />} />
      </div>
      <ExecutionIncidents incidents={data.engine.execution_incidents} />
      <div className="dashboard-grid">
        <div className="stack">
          <Card>
            <div className="card-header">
              <div><h2>实时持仓矩阵</h2><p>券商持仓与策略归属；手动或未归属数量单独列出</p></div>
              <Radio size={17} color="#3df6de" />
            </div>
            <PositionsTable snapshot={portfolio} />
          </Card>
          <Card>
            <div className="card-header"><div><h2>开放订单</h2><p>同一快照中的委托与保护单</p></div></div>
            <OrdersTable snapshot={portfolio} />
          </Card>
          <Card>
            <div className="card-header"><div><h2>最新策略信号</h2><p>条件树最近产生的入场与离场判断</p></div><Activity size={17} color="#a775ff" /></div>
            {data.signals.length ? <div className="table-scroll" style={{ paddingTop: 12 }}>
              <table className="data-table"><thead><tr><th>时间</th><th>代码</th><th>动作</th><th>参考价</th><th>状态</th><th>原因</th></tr></thead>
                <tbody>{data.signals.map((signal, index) => <tr key={`${signal.created_at}-${index}`}>
                  <td>{formatTime(signal.created_at)}</td><td className="symbol-cell">{signal.symbol}</td>
                  <td><Badge tone={actionTone(signal.action)}>{actionLabel(signal.action)}</Badge></td>
                  <td>{money(signal.price)}</td><td>{signal.status}</td><td>{signal.reason}</td>
                </tr>)}</tbody>
              </table></div> : <Empty title="尚未产生交易信号" detail="策略只会基于已经完成的K线触发，并使用唯一键避免重复下单。" />}
          </Card>
        </div>
        <Card>
          <div className="card-header"><div><h2>系统事件流</h2><p>连接、风控、订单和引擎事件</p></div><span className="system-pulse"><span /> LIVE</span></div>
          {data.events.length ? <div className="timeline">{data.events.map((event, index) => <div className="timeline-item" key={`${event.created_at}-${index}`}>
            <div className={`timeline-node ${event.level}`} />
            <div className="timeline-content"><strong>{event.message}</strong><p>{event.category.toUpperCase()} · {formatTime(event.created_at)}</p></div>
          </div>)}</div> : <Empty title="事件流等待中" detail={`最后心跳：${formatTime(data.engine.last_heartbeat)}`} />}
        </Card>
      </div>
    </>
  )
}
