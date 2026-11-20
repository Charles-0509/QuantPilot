import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertOctagon, Bot, CheckCircle, Pause, Play, RefreshCw, ShieldAlert, XCircle } from 'lucide-react'
import { api, formatTime, money } from '../api'
import { enginePresentation } from '../status'
import type { EngineStatus, EvaluationHealth, PortfolioSnapshot, Strategy } from '../types'
import { OrdersTable, SnapshotNotice } from '../components/PortfolioPanels'
import { Badge, Button, Card, Empty, ErrorPanel, Loading, PageHeader } from '../components/UI'
import ExecutionIncidents from '../components/ExecutionIncidents'

export default function Trading() {
  const client = useQueryClient()
  const engine = useQuery({ queryKey: ['engine'], queryFn: () => api<EngineStatus>('/api/engine'), refetchInterval: 5000 })
  const strategies = useQuery({ queryKey: ['strategies'], queryFn: () => api<Strategy[]>('/api/strategies') })
  const signals = useQuery({ queryKey: ['signals'], queryFn: () => api<any[]>('/api/signals?limit=80'), refetchInterval: 8000 })
  const portfolio = useQuery({ queryKey: ['portfolio'], queryFn: () => api<PortfolioSnapshot>('/api/portfolio'), retry: false, refetchInterval: 8000 })
  const action = useMutation({
    mutationFn: ({ path, reason }: { path: string; reason: string }) => api<any>(`/api/engine/${path}`, { method: 'POST', body: JSON.stringify({ reason }) }),
    onSuccess: () => { client.invalidateQueries({ queryKey: ['engine'] }); client.invalidateQueries({ queryKey: ['portfolio'] }) },
  })
  const clearIncident = useMutation({
    mutationFn: (symbol?: string) => api<any>('/api/engine/clear-incidents', { method: 'POST', body: JSON.stringify({ symbol }) }),
    onSuccess: () => { client.invalidateQueries({ queryKey: ['engine'] }); client.invalidateQueries({ queryKey: ['portfolio'] }) },
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
    <PageHeader eyebrow={engine.data?.paper === false ? 'AUTONOMOUS EXECUTION / LIVE' : 'AUTONOMOUS EXECUTION / PAPER'} title="自动交易" description="后台引擎使用已完成K线评估策略，通过唯一信号键、风险闸门和订单对账保证交易可追踪。" actions={<Badge tone={engineView.tone}>{engineView.label}</Badge>} />
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
    {activeIncidents.length > 0 && <div className="danger-callout" style={{ marginTop: 16, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
      <div>
        <strong><ShieldAlert size={16} style={{ display: 'inline', verticalAlign: 'middle', marginRight: 6 }} />执行安全隔离：</strong>
        {activeIncidents.join('、')} 检测到持仓或开放卖单不满足只做多约束。
      </div>
      <Button variant="secondary" disabled={clearIncident.isPending} onClick={() => clearIncident.mutate(undefined)}>
        <CheckCircle size={14} />确认安全并解除隔离
      </Button>
    </div>}
    <ExecutionIncidents incidents={engine.data?.execution_incidents} />
    {action.error && <div style={{ marginTop: 16 }}><ErrorPanel message={(action.error as Error).message} /></div>}
    {clearIncident.error && <div style={{ marginTop: 16 }}><ErrorPanel message={(clearIncident.error as Error).message} /></div>}
    <div className="two-column" style={{ marginTop: 16 }}>
      <Card><div className="card-header"><div><h2>已启用策略</h2><p>只有自定义策略可进入执行队列</p></div><Badge tone="info">{enabled.length}</Badge></div>
        {enabled.length ? <div className="card-pad stack">{enabled.map((strategy) => <div className="toggle-row" key={strategy.id}><div><strong>{strategy.name}</strong><p className="field-hint">{strategy.definition.symbols.join(' · ')} / {strategy.definition.timeframe}</p></div><Badge tone={engineView.strategyTone}>{engineView.strategyLabel}</Badge></div>)}</div> : <Empty title="没有启用策略" detail="请在策略库复制模板、完成回测并启用。" />}
      </Card>
      <Card><div className="card-header"><div><h2>开放订单</h2><p>与总览共用同一券商快照</p></div><RefreshCw size={16} color="#a775ff" /></div>
        {portfolio.isLoading ? <Loading label="正在确认开放订单" /> : portfolio.error ? <div style={{ padding: 16 }}><ErrorPanel message={(portfolio.error as Error).message} /></div> : <><div style={{ padding: '0 16px' }}><SnapshotNotice snapshot={portfolio.data} /></div><OrdersTable snapshot={portfolio.data} /></>}
      </Card>
    </div>
    <EvaluationHealthCard health={engine.data?.evaluation_health} />
    <Card style={{ marginTop: 16 } as any}><div className="card-header"><div><h2>信号与执行记录</h2><p>重复的策略ID、代码、K线时间和动作不会再次下单</p></div><AlertOctagon size={17} color="#f2bd5c" /></div>
      {signals.data?.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>时间</th><th>代码</th><th>动作</th><th>价格</th><th>执行状态</th><th>已成交数量</th><th>原因</th></tr></thead><tbody>{signals.data.map((signal) => <tr key={signal.id}><td>{formatTime(signal.created_at)}</td><td className="symbol-cell">{signal.symbol}</td><td><Badge tone={signal.action === 'buy' ? 'success' : 'warning'}>{signal.action}</Badge></td><td>{money(signal.price)}</td><td>{executionLabel(signal.execution_status || signal.status)}</td><td>{signal.filled_qty ?? '—'}</td><td>{signal.execution_error || signal.reason}</td></tr>)}</tbody></table></div> : <Empty title="尚无信号记录" />}
    </Card>
  </>
}

function executionLabel(status: string) {
  return ({ submitted: '已提交', filled: '已成交', partially_filled: '部分成交',
    partially_filled_canceled: '部分成交后撤单/到期', canceled: '已撤单', rejected: '已拒绝',
    pending_submission: '等待提交', pending_reconciliation: '等待对账', new: '待成交',
    accepted: '已受理', expired: '已到期' } as Record<string, string>)[status] || status
}

const OUTCOME_LABELS: Record<string, string> = {
  no_trigger: '条件未触发', entry_signal: '已生成买入信号', exit_signal: '已生成卖出信号',
  entry_matched_holding: '入场成立但已有持仓', insufficient_bars: 'K线不足', waiting_reconciliation: '等待订单对账', error: '评估出错',
}

function EvaluationHealthCard({ health }: { health?: EvaluationHealth }) {
  return <Card style={{ marginTop: 16 } as any}>
    <div className="card-header"><div><h2>策略评估心跳</h2><p>每个策略/标的最近一次检查；用于区分“条件没触发”和“评估停止”</p></div>
      {health && <Badge tone={health.stalled.length ? 'danger' : 'success'}>{health.stalled.length ? `${health.stalled.length} 项滞后` : '正常'}</Badge>}</div>
    {health?.rows.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>策略</th><th>代码</th><th>周期</th><th>最近K线</th><th>结果</th><th>最近检查</th></tr></thead>
      <tbody>{health.rows.map((row) => <tr key={`${row.strategy_id}-${row.symbol}`}>
        <td>{row.strategy_name}</td><td className="symbol-cell">{row.symbol}</td><td>{row.timeframe}</td><td>{formatTime(row.bar_timestamp)}</td>
        <td>{row.stalled ? <Badge tone="danger">滞后</Badge> : null} {OUTCOME_LABELS[row.outcome] || row.outcome}{row.error ? `：${row.error}` : ''}</td>
        <td>{formatTime(row.checked_at)}</td></tr>)}</tbody></table></div>
      : <Empty title="尚无评估记录" detail="引擎重启后，首次进入交易时段评估后才会出现；休市期间不评估。" />}
  </Card>
}
