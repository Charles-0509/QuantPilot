import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertOctagon, Bot, Pause, Play, RefreshCw, XCircle } from 'lucide-react'
import { api, formatTime, money } from '../api'
import { enginePresentation } from '../status'
import type { EngineStatus, Strategy } from '../types'
import { Badge, Button, Card, Empty, ErrorPanel, Loading, PageHeader } from '../components/UI'

export default function Trading() {
  const client = useQueryClient()
  const engine = useQuery({ queryKey: ['engine'], queryFn: () => api<EngineStatus>('/api/engine'), refetchInterval: 5000 })
  const strategies = useQuery({ queryKey: ['strategies'], queryFn: () => api<Strategy[]>('/api/strategies') })
  const signals = useQuery({ queryKey: ['signals'], queryFn: () => api<any[]>('/api/signals?limit=80'), refetchInterval: 8000 })
  const orders = useQuery({ queryKey: ['orders'], queryFn: () => api<any[]>('/api/orders?status=open'), retry: false, refetchInterval: 8000 })
  const action = useMutation({
    mutationFn: ({ path, reason }: { path: string; reason: string }) => api<any>(`/api/engine/${path}`, { method: 'POST', body: JSON.stringify({ reason }) }),
    onSuccess: () => { client.invalidateQueries({ queryKey: ['engine'] }); client.invalidateQueries({ queryKey: ['orders'] }) },
  })
  if (engine.isLoading) return <Loading label="正在读取交易引擎" />
  if (engine.error) return <ErrorPanel message={(engine.error as Error).message} />
  const running = engine.data?.status === 'running'
  const engineView = enginePresentation(engine.data)
  const activeIncidents = engine.data?.active_incidents || []
  const enabled = strategies.data?.filter((item) => item.enabled) || []
  const reactorColor = engineView.tone === 'success' ? '#3df6de' : engineView.tone === 'danger' ? '#ff647c' : '#f2bd5c'
  const engineReason = engineView.operationalStatus === 'paused'
    ? engine.data?.reason
    : engine.data?.operational_reason || engine.data?.reason

  return <>
    <PageHeader eyebrow="AUTONOMOUS EXECUTION / PAPER" title="自动交易" description="后台引擎使用已完成K线评估策略，通过唯一信号键、风险闸门和订单对账保证模拟交易可追踪。" actions={<Badge tone={engineView.tone}>{engineView.label}</Badge>} />
    <Card className="engine-hero">
      <div className="engine-visual"><div className="reactor"><Bot size={29} color={reactorColor} /></div><div className="engine-copy"><h2>{engineView.title}</h2><p>{engineReason} · 最后心跳 {formatTime(engine.data?.last_heartbeat)}</p></div></div>
      <div className="engine-actions">
        <Button disabled={running || action.isPending || activeIncidents.length > 0} onClick={() => action.mutate({ path: 'resume', reason: '用户从自动交易页面开启' })}><Play size={15} />启动引擎</Button>
        <Button variant="secondary" disabled={!running || action.isPending} onClick={() => action.mutate({ path: 'pause', reason: '用户从自动交易页面暂停' })}><Pause size={15} />暂停并撤单</Button>
        <Button variant="ghost" onClick={() => action.mutate({ path: 'cancel-orders', reason: '用户取消全部订单' })}><XCircle size={15} />取消开放订单</Button>
      </div>
    </Card>
    {running && !engineView.active && <div className={engineView.tone === 'danger' ? 'danger-callout' : 'warning-callout'} style={{ marginTop: 16 }}>
      当前策略仍保持启用，但系统不会把连接异常误报为正常执行。连接恢复后将自动继续评估，无需重复启动引擎。
    </div>}
    {activeIncidents.length > 0 && <div className="danger-callout" style={{ marginTop: 16 }}>
      <strong>执行安全隔离：</strong>{activeIncidents.join('、')} 检测到持仓或开放卖单不满足只做多约束。系统正在自动取消 QuantPilot 自有订单并用 REST 复核；确认安全前不能恢复引擎。
    </div>}
    {action.error && <div style={{ marginTop: 16 }}><ErrorPanel message={(action.error as Error).message} /></div>}
    <div className="two-column" style={{ marginTop: 16 }}>
      <Card><div className="card-header"><div><h2>已启用策略</h2><p>只有自定义策略可进入执行队列</p></div><Badge tone="info">{enabled.length}</Badge></div>
        {enabled.length ? <div className="card-pad stack">{enabled.map((strategy) => <div className="toggle-row" key={strategy.id}><div><strong>{strategy.name}</strong><p className="field-hint">{strategy.definition.symbols.join(' · ')} / {strategy.definition.timeframe}</p></div><Badge tone={engineView.strategyTone}>{engineView.strategyLabel}</Badge></div>)}</div> : <Empty title="没有启用策略" detail="请在策略库复制模板、完成回测并启用。" />}
      </Card>
      <Card><div className="card-header"><div><h2>开放订单</h2><p>数据来自 Alpaca Paper Trading</p></div><RefreshCw size={16} color="#a775ff" /></div>
        {orders.isLoading ? <Loading label="正在确认开放订单" /> : orders.error ? <div style={{ padding: 16 }}><ErrorPanel message={(orders.error as Error).message} /></div> : orders.data?.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>代码</th><th>方向</th><th>类型</th><th>数量</th><th>状态</th></tr></thead><tbody>{orders.data.map((order) => <tr key={order.id}><td className="symbol-cell">{order.symbol}</td><td>{order.side}</td><td>{order.type}</td><td>{order.qty || order.notional}</td><td>{order.status}</td></tr>)}</tbody></table></div> : <Empty title="没有开放订单" />}
      </Card>
    </div>
    <Card style={{ marginTop: 16 } as any}><div className="card-header"><div><h2>信号与执行记录</h2><p>重复的策略ID、代码、K线时间和动作不会再次下单</p></div><AlertOctagon size={17} color="#f2bd5c" /></div>
      {signals.data?.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>时间</th><th>代码</th><th>动作</th><th>价格</th><th>状态</th><th>原因</th></tr></thead><tbody>{signals.data.map((signal) => <tr key={signal.id}><td>{formatTime(signal.created_at)}</td><td className="symbol-cell">{signal.symbol}</td><td><Badge tone={signal.action === 'buy' ? 'success' : 'warning'}>{signal.action}</Badge></td><td>{money(signal.price)}</td><td>{signal.status}</td><td>{signal.reason}</td></tr>)}</tbody></table></div> : <Empty title="尚无信号记录" />}
    </Card>
  </>
}
