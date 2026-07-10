import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertOctagon, Bot, Pause, Play, RefreshCw, XCircle } from 'lucide-react'
import { api, formatTime, money } from '../api'
import type { Strategy } from '../types'
import { Badge, Button, Card, Empty, ErrorPanel, Loading, PageHeader } from '../components/UI'

export default function Trading() {
  const client = useQueryClient()
  const engine = useQuery({ queryKey: ['engine'], queryFn: () => api<any>('/api/engine'), refetchInterval: 5000 })
  const strategies = useQuery({ queryKey: ['strategies'], queryFn: () => api<Strategy[]>('/api/strategies') })
  const signals = useQuery({ queryKey: ['signals'], queryFn: () => api<any[]>('/api/signals?limit=80'), refetchInterval: 8000 })
  const orders = useQuery({ queryKey: ['orders'], queryFn: () => api<any[]>('/api/orders?status=open'), retry: false, refetchInterval: 8000 })
  const action = useMutation({
    mutationFn: ({ path, reason }: { path: string; reason: string }) => api<any>(`/api/engine/${path}`, { method: 'POST', body: JSON.stringify({ reason }) }),
    onSuccess: () => { client.invalidateQueries({ queryKey: ['engine'] }); client.invalidateQueries({ queryKey: ['orders'] }) },
  })
  if (engine.isLoading) return <Loading label="正在读取交易引擎" />
  const running = engine.data?.status === 'running'
  const enabled = strategies.data?.filter((item) => item.enabled) || []

  return <>
    <PageHeader eyebrow="AUTONOMOUS EXECUTION / PAPER" title="自动交易" description="后台引擎使用已完成K线评估策略，通过唯一信号键、风险闸门和订单对账保证模拟交易可追踪。" actions={<Badge tone={running ? 'success' : 'warning'}>{running ? '引擎运行中' : '引擎已暂停'}</Badge>} />
    <Card className="engine-hero">
      <div className="engine-visual"><div className="reactor"><Bot size={29} color="#3df6de" /></div><div className="engine-copy"><h2>{running ? '策略反应堆正在运行' : '策略反应堆处于安全暂停'}</h2><p>{engine.data?.reason} · 最后心跳 {formatTime(engine.data?.last_heartbeat)}</p></div></div>
      <div className="engine-actions">
        <Button disabled={running || action.isPending} onClick={() => action.mutate({ path: 'resume', reason: '用户从自动交易页面开启' })}><Play size={15} />启动引擎</Button>
        <Button variant="secondary" disabled={!running || action.isPending} onClick={() => action.mutate({ path: 'pause', reason: '用户从自动交易页面暂停' })}><Pause size={15} />暂停并撤单</Button>
        <Button variant="ghost" onClick={() => action.mutate({ path: 'cancel-orders', reason: '用户取消全部订单' })}><XCircle size={15} />取消开放订单</Button>
      </div>
    </Card>
    {action.error && <div style={{ marginTop: 16 }}><ErrorPanel message={(action.error as Error).message} /></div>}
    <div className="two-column" style={{ marginTop: 16 }}>
      <Card><div className="card-header"><div><h2>已启用策略</h2><p>只有自定义策略可进入执行队列</p></div><Badge tone="info">{enabled.length}</Badge></div>
        {enabled.length ? <div className="card-pad stack">{enabled.map((strategy) => <div className="toggle-row" key={strategy.id}><div><strong>{strategy.name}</strong><p className="field-hint">{strategy.definition.symbols.join(' · ')} / {strategy.definition.timeframe}</p></div><Badge tone="success">ACTIVE</Badge></div>)}</div> : <Empty title="没有启用策略" detail="请在策略库复制模板、完成回测并启用。" />}
      </Card>
      <Card><div className="card-header"><div><h2>开放订单</h2><p>数据来自 Alpaca Paper Trading</p></div><RefreshCw size={16} color="#a775ff" /></div>
        {orders.data?.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>代码</th><th>方向</th><th>类型</th><th>数量</th><th>状态</th></tr></thead><tbody>{orders.data.map((order) => <tr key={order.id}><td className="symbol-cell">{order.symbol}</td><td>{order.side}</td><td>{order.type}</td><td>{order.qty || order.notional}</td><td>{order.status}</td></tr>)}</tbody></table></div> : <Empty title="没有开放订单" />}
      </Card>
    </div>
    <Card style={{ marginTop: 16 } as any}><div className="card-header"><div><h2>信号与执行记录</h2><p>重复的策略ID、代码、K线时间和动作不会再次下单</p></div><AlertOctagon size={17} color="#f2bd5c" /></div>
      {signals.data?.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>时间</th><th>代码</th><th>动作</th><th>价格</th><th>状态</th><th>原因</th></tr></thead><tbody>{signals.data.map((signal) => <tr key={signal.id}><td>{formatTime(signal.created_at)}</td><td className="symbol-cell">{signal.symbol}</td><td><Badge tone={signal.action === 'buy' ? 'success' : 'warning'}>{signal.action}</Badge></td><td>{money(signal.price)}</td><td>{signal.status}</td><td>{signal.reason}</td></tr>)}</tbody></table></div> : <Empty title="尚无信号记录" />}
    </Card>
  </>
}
