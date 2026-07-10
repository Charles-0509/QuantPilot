import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Code2, Save, ShieldCheck } from 'lucide-react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import type { RuleDefinition, Strategy } from '../types'
import RuleBuilder from '../components/RuleBuilder'
import { Badge, Button, Card, ErrorPanel, Field, Loading, PageHeader } from '../components/UI'

const defaultDefinition: RuleDefinition = {
  version: 1, name: '我的量化策略', description: '使用条件卡片创建的自定义策略。', symbols: ['SPY'], timeframe: '15Min', warmup_bars: 220,
  schedule: { session: 'regular', weekdays: [0, 1, 2, 3, 4] },
  entry: { type: 'group', op: 'AND', negate: false, children: [{ type: 'condition', left: { kind: 'price', field: 'close' }, operator: 'crosses_above', right: { kind: 'indicator', indicator: 'SMA', field: 'value', params: { period: 20 } }, label: '价格上穿 SMA20' }] },
  exit: { type: 'group', op: 'AND', negate: false, children: [{ type: 'condition', left: { kind: 'price', field: 'close' }, operator: 'crosses_below', right: { kind: 'indicator', indicator: 'SMA', field: 'value', params: { period: 20 } }, label: '价格下穿 SMA20' }] },
  position: { mode: 'percent_equity', value: 10, allow_pyramiding: false, max_additions: 1 },
  order: { type: 'market', limit_offset_bps: 10, time_in_force: 'day', stop_loss: { mode: 'percent', value: 2, atr_period: 14 }, take_profit: { mode: 'percent', value: 4, atr_period: 14 }, trailing_stop: null },
  risk: { max_symbol_pct: 10, max_positions: 8, cooldown_bars: 2 },
}

export default function StrategyEditor() {
  const { id } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const strategy = useQuery({ queryKey: ['strategy', id], queryFn: () => api<Strategy>(`/api/strategies/${id}`), enabled: Boolean(id) })
  const [definition, setDefinition] = useState<RuleDefinition>(defaultDefinition)
  const [symbolsText, setSymbolsText] = useState('SPY')
  useEffect(() => { if (strategy.data) { setDefinition(strategy.data.definition); setSymbolsText(strategy.data.definition.symbols.join(', ')) } }, [strategy.data])
  const save = useMutation({
    mutationFn: () => api<Strategy>(id ? `/api/strategies/${id}` : '/api/strategies', { method: id ? 'PUT' : 'POST', body: JSON.stringify({ definition: { ...definition, symbols: symbolsText.split(',').map((item) => item.trim().toUpperCase()).filter(Boolean) } }) }),
    onSuccess: (saved) => { queryClient.invalidateQueries({ queryKey: ['strategies'] }); navigate(`/strategies/${saved.id}`, { replace: true }) },
  })
  if (id && strategy.isLoading) return <Loading label="正在读取策略定义" />
  if (strategy.error) return <ErrorPanel message={(strategy.error as Error).message} />
  const readOnly = strategy.data?.is_template
  const bracket = Boolean(definition.order.stop_loss && definition.order.take_profit)
  const trailing = Boolean(definition.order.trailing_stop)

  return <>
    <PageHeader eyebrow="RULE DEFINITION V1" title={id ? '策略规则编辑器' : '创建量化策略'} description="使用递归条件树组合价格、成交量和技术指标。所有条件在完成K线后计算。" actions={<>
      <Link to="/strategies"><Button variant="ghost"><ArrowLeft size={14} />返回策略库</Button></Link>
      <Button disabled={save.isPending || readOnly} onClick={() => save.mutate()}><Save size={14} />保存新版本</Button>
    </>} />
    {readOnly && <div className="warning-callout" style={{ marginBottom: 16 }}>内置模板保持只读。请从策略库点击“复制并编辑”。</div>}
    {save.error && <div style={{ marginBottom: 16 }}><ErrorPanel message={(save.error as Error).message} /></div>}
    <div className="editor-layout">
      <div className="stack">
        <Card>
          <div className="form-section">
            <div className="form-section-title"><div><h3>策略身份与市场</h3><p>定义策略名称、股票池、时间周期和指标预热长度。</p></div><Badge tone="info">PAPER ONLY</Badge></div>
            <div className="form-grid"><Field label="策略名称"><input value={definition.name} disabled={readOnly} onChange={(e) => setDefinition({ ...definition, name: e.target.value })} /></Field>
              <Field label="股票池" hint="英文逗号分隔，启用策略合计最多30个代码"><input value={symbolsText} disabled={readOnly} onChange={(e) => setSymbolsText(e.target.value.toUpperCase())} /></Field></div>
            <Field label="策略说明"><textarea value={definition.description} disabled={readOnly} onChange={(e) => setDefinition({ ...definition, description: e.target.value })} /></Field>
            <div className="form-grid-3"><Field label="K线周期"><select value={definition.timeframe} disabled={readOnly} onChange={(e) => setDefinition({ ...definition, timeframe: e.target.value as RuleDefinition['timeframe'] })}><option value="5Min">5分钟</option><option value="15Min">15分钟</option><option value="30Min">30分钟</option><option value="1Hour">1小时</option><option value="1Day">日线</option></select></Field>
              <Field label="预热K线"><input type="number" value={definition.warmup_bars} disabled={readOnly} onChange={(e) => setDefinition({ ...definition, warmup_bars: Number(e.target.value) })} /></Field>
              <Field label="交易时段"><input value="美股常规交易时段" disabled /></Field></div>
          </div>
        </Card>
        <Card><div className="form-section"><RuleBuilder title="入场条件" value={definition.entry} onChange={(entry) => setDefinition({ ...definition, entry })} /></div></Card>
        <Card><div className="form-section"><RuleBuilder title="离场条件" value={definition.exit} onChange={(exit) => setDefinition({ ...definition, exit })} /></div></Card>
        <Card>
          <div className="form-section">
            <div className="form-section-title"><div><h3>仓位与订单</h3><p>模拟盘只做多；信号生成后在下一次执行阶段提交订单。</p></div></div>
            <div className="form-grid-3"><Field label="仓位模式"><select value={definition.position.mode} onChange={(e) => setDefinition({ ...definition, position: { ...definition.position, mode: e.target.value as any } })}><option value="percent_equity">账户净值百分比</option><option value="fixed_notional">固定金额</option><option value="fixed_qty">固定股数</option><option value="risk_based">按止损风险百分比</option></select></Field>
              <Field label="仓位数值"><input type="number" step="0.1" value={definition.position.value} onChange={(e) => setDefinition({ ...definition, position: { ...definition.position, value: Number(e.target.value) } })} /></Field>
              <Field label="订单类型"><select value={definition.order.type} onChange={(e) => setDefinition({ ...definition, order: { ...definition.order, type: e.target.value as 'market' | 'limit' } })}><option value="market">市价单</option><option value="limit">限价单</option></select></Field></div>
            {definition.order.type === 'limit' && <Field label="限价偏移（基点）"><input type="number" value={definition.order.limit_offset_bps} onChange={(e) => setDefinition({ ...definition, order: { ...definition.order, limit_offset_bps: Number(e.target.value) } })} /></Field>}
            <div className="toggle-row"><div><strong>允许追加仓位</strong><p className="field-hint">适合定投；达到最大追加次数或仓位上限后停止。</p></div><button className={`toggle ${definition.position.allow_pyramiding ? 'on' : ''}`} onClick={() => setDefinition({ ...definition, position: { ...definition.position, allow_pyramiding: !definition.position.allow_pyramiding } })}><span /></button></div>
          </div>
          <div className="form-section">
            <div className="form-section-title"><div><h3>保护性订单</h3><p>Bracket 与移动止损互斥；前后端都会进行校验。</p></div><ShieldCheck size={18} color="#3df6de" /></div>
            <div className="toggle-row"><div><strong>Bracket 止损止盈</strong><p className="field-hint">买单成交后由 Alpaca 同时管理止损和止盈。</p></div><button className={`toggle ${bracket ? 'on' : ''}`} onClick={() => setDefinition({ ...definition, order: { ...definition.order, stop_loss: bracket ? null : { mode: 'percent', value: 2, atr_period: 14 }, take_profit: bracket ? null : { mode: 'percent', value: 4, atr_period: 14 }, trailing_stop: null } })}><span /></button></div>
            {bracket && <div className="form-grid"><Guard label="止损" value={definition.order.stop_loss!} onChange={(stop_loss) => setDefinition({ ...definition, order: { ...definition.order, stop_loss } })} /><Guard label="止盈" value={definition.order.take_profit!} onChange={(take_profit) => setDefinition({ ...definition, order: { ...definition.order, take_profit } })} /></div>}
            <div className="toggle-row"><div><strong>独立移动止损</strong><p className="field-hint">买单成交后提交模拟盘 trailing stop。</p></div><button className={`toggle ${trailing ? 'on' : ''}`} onClick={() => setDefinition({ ...definition, order: { ...definition.order, trailing_stop: trailing ? null : { mode: 'percent', value: 3 }, stop_loss: null, take_profit: null } })}><span /></button></div>
            {trailing && <div className="form-grid"><Field label="移动方式"><select value={definition.order.trailing_stop!.mode} onChange={(e) => setDefinition({ ...definition, order: { ...definition.order, trailing_stop: { ...definition.order.trailing_stop!, mode: e.target.value as 'percent' | 'price' } } })}><option value="percent">百分比</option><option value="price">固定美元距离</option></select></Field><Field label="移动距离"><input type="number" step="0.1" value={definition.order.trailing_stop!.value} onChange={(e) => setDefinition({ ...definition, order: { ...definition.order, trailing_stop: { ...definition.order.trailing_stop!, value: Number(e.target.value) } } })} /></Field></div>}
          </div>
          <div className="form-section"><div className="form-section-title"><div><h3>策略级风控</h3><p>全局风险中心仍会应用更严格的限制。</p></div></div>
            <div className="form-grid-3"><Field label="单股票上限 %"><input type="number" value={definition.risk.max_symbol_pct} onChange={(e) => setDefinition({ ...definition, risk: { ...definition.risk, max_symbol_pct: Number(e.target.value) } })} /></Field><Field label="最大持仓数"><input type="number" value={definition.risk.max_positions} onChange={(e) => setDefinition({ ...definition, risk: { ...definition.risk, max_positions: Number(e.target.value) } })} /></Field><Field label="冷却K线数"><input type="number" value={definition.risk.cooldown_bars} onChange={(e) => setDefinition({ ...definition, risk: { ...definition.risk, cooldown_bars: Number(e.target.value) } })} /></Field></div>
          </div>
        </Card>
      </div>
      <Card className="editor-sidebar"><div className="card-header"><div><h2>规则 JSON 预览</h2><p>保存后使用 RuleDefinition v1 解释执行</p></div><Code2 size={17} color="#a775ff" /></div><div className="card-pad"><pre className="json-preview">{JSON.stringify({ ...definition, symbols: symbolsText.split(',').map((item) => item.trim().toUpperCase()).filter(Boolean) }, null, 2)}</pre></div></Card>
    </div>
  </>
}

function Guard({ label, value, onChange }: { label: string; value: NonNullable<RuleDefinition['order']['stop_loss']>; onChange: (value: NonNullable<RuleDefinition['order']['stop_loss']>) => void }) {
  return <div className="form-grid"><Field label={`${label}模式`}><select value={value.mode} onChange={(e) => onChange({ ...value, mode: e.target.value as 'percent' | 'atr' })}><option value="percent">百分比</option><option value="atr">ATR倍数</option></select></Field><Field label={`${label}数值`}><input type="number" step="0.1" value={value.value} onChange={(e) => onChange({ ...value, value: Number(e.target.value) })} /></Field></div>
}
