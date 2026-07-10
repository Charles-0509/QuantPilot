import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, Play, Plus, Settings2, Sparkles, Square } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import type { Strategy } from '../types'
import { Badge, Button, Card, Empty, ErrorPanel, Loading, PageHeader } from '../components/UI'

export default function Strategies() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const strategies = useQuery({ queryKey: ['strategies'], queryFn: () => api<Strategy[]>('/api/strategies') })
  const mutate = useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'clone' | 'enable' | 'disable' }) => api<Strategy>(`/api/strategies/${id}/${action}`, { method: 'POST' }),
    onSuccess: (strategy, variables) => { queryClient.invalidateQueries({ queryKey: ['strategies'] }); if (variables.action === 'clone') navigate(`/strategies/${strategy.id}`) },
  })
  if (strategies.isLoading) return <Loading label="正在加载策略库" />
  if (strategies.error) return <ErrorPanel message={(strategies.error as Error).message} />
  const templates = strategies.data?.filter((item) => item.is_template) || []
  const custom = strategies.data?.filter((item) => !item.is_template) || []

  return <>
    <PageHeader eyebrow="STRATEGY MATRIX" title="策略库" description="从常见规则模板开始，复制后使用条件卡片调整指标、股票池、仓位、订单和风控。" actions={<Link to="/strategies/new"><Button><Plus size={15} />新建空白策略</Button></Link>} />
    <div className="card-header" style={{ padding: '0 0 13px' }}><div><h2>我的策略</h2><p>只有复制或新建的策略可以启用自动交易</p></div></div>
    {custom.length ? <div className="strategy-grid" style={{ marginBottom: 32 }}>{custom.map((strategy) => <StrategyCard key={strategy.id} strategy={strategy} onAction={(action) => mutate.mutate({ id: strategy.id, action })} />)}</div> : <Card style={{ marginBottom: 32 } as any}><Empty title="还没有自定义策略" detail="复制一个模板或创建空白策略，完成回测后再启用模拟交易。" /></Card>}
    <div className="card-header" style={{ padding: '0 0 13px' }}><div><h2>内置规则模板</h2><p>模板保持只读，复制后可自由修改</p></div><Badge tone="info">{templates.length} 个模板</Badge></div>
    <div className="strategy-grid">{templates.map((strategy) => <StrategyCard key={strategy.id} strategy={strategy} onAction={(action) => mutate.mutate({ id: strategy.id, action })} />)}</div>
    {mutate.error && <div style={{ marginTop: 16 }}><ErrorPanel message={(mutate.error as Error).message} /></div>}
  </>
}

function StrategyCard({ strategy, onAction }: { strategy: Strategy; onAction: (action: 'clone' | 'enable' | 'disable') => void }) {
  return <Card className="strategy-card">
    <div className="strategy-card-top"><div className="strategy-orb">{strategy.is_template ? <Sparkles size={19} /> : <Settings2 size={19} />}</div>
      <Badge tone={strategy.is_template ? 'info' : strategy.enabled ? 'success' : 'neutral'}>{strategy.is_template ? '内置模板' : strategy.enabled ? '运行中' : '未启用'}</Badge></div>
    <h3>{strategy.name}</h3><p>{strategy.description}</p>
    <div className="strategy-meta"><Badge>{strategy.definition.timeframe}</Badge><Badge>{strategy.definition.symbols.join(' · ')}</Badge><Badge>v{strategy.version}</Badge></div>
    <div className="strategy-actions">
      {strategy.is_template ? <Button variant="secondary" onClick={() => onAction('clone')}><Copy size={14} />复制并编辑</Button> : <>
        <Link to={`/strategies/${strategy.id}`}><Button variant="ghost"><Settings2 size={14} />编辑</Button></Link>
        <Button variant={strategy.enabled ? 'danger' : 'primary'} onClick={() => onAction(strategy.enabled ? 'disable' : 'enable')}>
          {strategy.enabled ? <Square size={13} /> : <Play size={13} />}{strategy.enabled ? '停止' : '启用'}
        </Button></>}
    </div>
  </Card>
}
