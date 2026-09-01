import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BarChart3, Copy, Filter, Play, Plus, Settings2, Sparkles, Square, Trash2 } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import type { Strategy } from '../types'
import { Badge, Button, Card, Empty, ErrorPanel, Loading, PageHeader } from '../components/UI'

const PRESET_GROUPS = [
  { label: '全部', value: 'ALL' },
  { label: '运行中', value: 'RUNNING' },
  { label: '未启用', value: 'DISABLED' },
  { label: '15分钟线', value: '15Min' },
  { label: '日线', value: '1Day' },
]

export default function Strategies() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [filter, setFilter] = useState('ALL')
  const [search, setSearch] = useState('')

  const strategies = useQuery({ queryKey: ['strategies'], queryFn: () => api<Strategy[]>('/api/strategies') })
  const mutate = useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'clone' | 'enable' | 'disable' | 'delete' }) => {
      if (action === 'delete') {
        return api<any>(`/api/strategies/${id}`, { method: 'DELETE' })
      }
      return api<Strategy>(`/api/strategies/${id}/${action}`, { method: 'POST' })
    },
    onSuccess: (strategy, variables) => {
      queryClient.invalidateQueries({ queryKey: ['strategies'] })
      if (variables.action === 'clone' && strategy?.id) {
        navigate(`/strategies/${strategy.id}`)
      }
    },
  })

  if (strategies.isLoading) return <Loading label="正在加载策略库" />
  if (strategies.error) return <ErrorPanel message={(strategies.error as Error).message} />

  const allStrategies = strategies.data || []
  const filterFn = (item: Strategy) => {
    if (search && !item.name.toLowerCase().includes(search.toLowerCase()) && !item.definition.symbols.some(s => s.toLowerCase().includes(search.toLowerCase()))) {
      return false
    }
    if (filter === 'RUNNING') return item.enabled
    if (filter === 'DISABLED') return !item.enabled && !item.is_template
    if (filter === '15Min') return item.definition.timeframe === '15Min'
    if (filter === '1Day') return item.definition.timeframe === '1Day'
    return true
  }

  const custom = allStrategies.filter((item) => !item.is_template).filter(filterFn)
  const templates = allStrategies.filter((item) => item.is_template).filter(filterFn)

  return <>
    <PageHeader
      eyebrow="STRATEGY MATRIX"
      title="策略库"
      description="从常见规则模板开始，复制后使用条件卡片调整指标、股票池、仓位、订单和风控。"
      actions={
        <div style={{ display: 'flex', gap: 10 }}>
          <Link to="/strategies/new"><Button><Plus size={15} />新建空白策略</Button></Link>
        </div>
      }
    />

    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20, flexWrap: 'wrap', gap: 12 }}>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {PRESET_GROUPS.map((g) => (
          <button
            key={g.value}
            className={`badge ${filter === g.value ? 'badge-info' : 'badge-neutral'}`}
            style={{ cursor: 'pointer', padding: '6px 12px', fontSize: 13, border: 'none' }}
            onClick={() => setFilter(g.value)}
          >
            {g.label}
          </button>
        ))}
      </div>
      <input
        type="text"
        placeholder="搜索策略名称或代码..."
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        style={{
          background: 'rgba(255, 255, 255, 0.05)',
          border: '1px solid rgba(255, 255, 255, 0.1)',
          borderRadius: 6,
          padding: '6px 12px',
          color: '#fff',
          width: 220,
          fontSize: 13,
        }}
      />
    </div>

    <div className="card-header" style={{ padding: '0 0 13px' }}>
      <div>
        <h2>我的策略</h2>
        <p>自定义策略支持一键快速回测、编辑与实盘模拟启停</p>
      </div>
      <Badge tone="info">{custom.length} 个策略</Badge>
    </div>

    {custom.length ? (
      <div className="strategy-grid" style={{ marginBottom: 32 }}>
        {custom.map((strategy) => (
          <StrategyCard
            key={strategy.id}
            strategy={strategy}
            onAction={(action) => mutate.mutate({ id: strategy.id, action })}
            onQuickBacktest={() => navigate(`/backtests?strategy=${strategy.id}&symbol=${strategy.definition.symbols[0] || 'SPY'}`)}
          />
        ))}
      </div>
    ) : (
      <Card style={{ marginBottom: 32 } as any}>
        <Empty title="没有符合条件的自定义策略" detail="复制内置模板或创建空白策略，完成回测后再启用模拟交易。" />
      </Card>
    )}

    <div className="card-header" style={{ padding: '0 0 13px' }}>
      <div>
        <h2>内置规则模板</h2>
        <p>经典量化交易策略模板，复制后可自由调整参数或拓展股票池</p>
      </div>
      <Badge tone="info">{templates.length} 个模板</Badge>
    </div>

    {templates.length ? (
      <div className="strategy-grid">
        {templates.map((strategy) => (
          <StrategyCard
            key={strategy.id}
            strategy={strategy}
            onAction={(action) => mutate.mutate({ id: strategy.id, action })}
            onQuickBacktest={() => navigate(`/backtests?strategy=${strategy.id}&symbol=${strategy.definition.symbols[0] || 'SPY'}`)}
          />
        ))}
      </div>
    ) : (
      <Card>
        <Empty title="无匹配模板" detail="尝试切换筛选器查看全部模板。" />
      </Card>
    )}

    {mutate.error && <div style={{ marginTop: 16 }}><ErrorPanel message={(mutate.error as Error).message} /></div>}
  </>
}

function StrategyCard({
  strategy,
  onAction,
  onQuickBacktest,
}: {
  strategy: Strategy
  onAction: (action: 'clone' | 'enable' | 'disable' | 'delete') => void
  onQuickBacktest: () => void
}) {
  return (
    <Card className="strategy-card">
      <div className="strategy-card-top">
        <div className="strategy-orb">{strategy.is_template ? <Sparkles size={19} /> : <Settings2 size={19} />}</div>
        <Badge tone={strategy.is_template ? 'info' : strategy.enabled ? 'success' : 'neutral'}>
          {strategy.is_template ? '内置模板' : strategy.enabled ? '运行中' : '未启用'}
        </Badge>
      </div>
      <h3>{strategy.name}</h3>
      <p style={{ minHeight: 38 }}>{strategy.description}</p>
      <div className="strategy-meta">
        <Badge>{strategy.definition.timeframe}</Badge>
        <Badge>{strategy.definition.symbols.join(' · ')}</Badge>
      </div>
      <div className="strategy-actions" style={{ marginTop: 14, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        {strategy.is_template ? (
          <>
            <Button variant="secondary" onClick={() => onAction('clone')}>
              <Copy size={14} />复制并编辑
            </Button>
            <Button variant="ghost" onClick={onQuickBacktest}>
              <BarChart3 size={14} />快速回测
            </Button>
          </>
        ) : (
          <>
            <Link to={`/strategies/${strategy.id}`}>
              <Button variant="ghost"><Settings2 size={14} />编辑</Button>
            </Link>
            <Button variant="ghost" onClick={onQuickBacktest}>
              <BarChart3 size={14} />回测
            </Button>
            <Button
              variant={strategy.enabled ? 'danger' : 'primary'}
              onClick={() => onAction(strategy.enabled ? 'disable' : 'enable')}
            >
              {strategy.enabled ? <Square size={13} /> : <Play size={13} />}
              {strategy.enabled ? '停止' : '启用'}
            </Button>
            {!strategy.enabled && (
              <Button
                variant="ghost"
                style={{ color: '#ff647c' }}
                onClick={() => {
                  if (window.confirm(`确定要删除策略“${strategy.name}”吗？`)) {
                    onAction('delete')
                  }
                }}
              >
                <Trash2 size={13} />
              </Button>
            )}
          </>
        )}
      </div>
    </Card>
  )
}
