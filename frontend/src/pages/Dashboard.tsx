import { useQuery } from '@tanstack/react-query'
import { Activity, CircleDollarSign, Landmark, Power, Radio, WalletCards } from 'lucide-react'
import { api, formatTime, money, number } from '../api'
import type { DashboardData } from '../types'
import { Badge, Card, Empty, ErrorPanel, Loading, PageHeader, StatCard } from '../components/UI'

export default function Dashboard() {
  const query = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => api<DashboardData>('/api/dashboard'),
    refetchInterval: 10000,
  })
  if (query.isLoading) return <Loading label="正在同步模拟账户" />
  if (query.error) return <ErrorPanel message={(query.error as Error).message} />
  const data = query.data!
  const account = data.account || {}
  const equity = Number(account.equity || 0)
  const lastEquity = Number(account.last_equity || equity || 0)
  const dayChange = lastEquity ? (equity / lastEquity - 1) * 100 : 0

  return (
    <>
      <PageHeader
        eyebrow="MISSION CONTROL / PAPER ENVIRONMENT"
        title="量化交易控制台"
        description="集中查看 Alpaca 模拟账户、策略引擎、持仓、信号与系统健康状态。所有订单均被永久锁定在 Paper Trading。"
        actions={<Badge tone={data.clock?.is_open ? 'success' : 'neutral'}>{data.clock?.is_open ? '美股交易中' : '美股已休市'}</Badge>}
      />
      {!data.connection.connected && (
        <div className="warning-callout" style={{ marginBottom: 16 }}>
          {data.connection.message}。应用和策略模板已经可以浏览；配置模拟盘密钥后即可获取行情、回测并自动交易。
        </div>
      )}
      <div className="stat-grid">
        <StatCard label="账户净值" value={money(account.equity)} trend={dayChange} detail="相对上一交易日" icon={<Landmark size={18} />} />
        <StatCard label="可用购买力" value={money(account.buying_power)} detail="Alpaca Paper" icon={<CircleDollarSign size={18} />} />
        <StatCard label="当前持仓" value={`${data.positions.length} 只`} detail={`未成交订单 ${data.orders.length} 笔`} icon={<WalletCards size={18} />} />
        <StatCard label="交易引擎" value={data.engine.status === 'running' ? '运行中' : '已暂停'} detail={data.engine.reason || '等待操作'} icon={<Power size={18} />} />
      </div>
      <div className="dashboard-grid">
        <div className="stack">
          <Card>
            <div className="card-header">
              <div><h2>实时持仓矩阵</h2><p>模拟盘资产、成本、浮动盈亏和当前市值</p></div>
              <Radio size={17} color="#3df6de" />
            </div>
            {data.positions.length ? (
              <div className="table-scroll" style={{ paddingTop: 12 }}>
                <table className="data-table">
                  <thead><tr><th>代码</th><th>数量</th><th>成本</th><th>现价</th><th>市值</th><th>未实现盈亏</th></tr></thead>
                  <tbody>{data.positions.map((position) => {
                    const pnl = Number(position.unrealized_pl || 0)
                    return <tr key={String(position.symbol)}>
                      <td className="symbol-cell">{position.symbol}</td>
                      <td>{number(position.qty, 4)}</td>
                      <td>{money(position.avg_entry_price)}</td>
                      <td>{money(position.current_price)}</td>
                      <td>{money(position.market_value)}</td>
                      <td className={pnl >= 0 ? 'positive' : 'negative'}>{money(pnl)}</td>
                    </tr>
                  })}</tbody>
                </table>
              </div>
            ) : <Empty title="暂时没有模拟持仓" detail="启用策略并启动交易引擎后，新的 Paper 订单会显示在这里。" />}
          </Card>
          <Card>
            <div className="card-header"><div><h2>最新策略信号</h2><p>条件树最近产生的入场与离场判断</p></div><Activity size={17} color="#a775ff" /></div>
            {data.signals.length ? <div className="table-scroll" style={{ paddingTop: 12 }}>
              <table className="data-table"><thead><tr><th>时间</th><th>代码</th><th>动作</th><th>参考价</th><th>状态</th><th>原因</th></tr></thead>
                <tbody>{data.signals.map((signal, index) => <tr key={`${signal.created_at}-${index}`}>
                  <td>{formatTime(signal.created_at)}</td><td className="symbol-cell">{signal.symbol}</td>
                  <td><Badge tone={signal.action === 'buy' ? 'success' : 'warning'}>{signal.action === 'buy' ? '买入' : '卖出'}</Badge></td>
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
