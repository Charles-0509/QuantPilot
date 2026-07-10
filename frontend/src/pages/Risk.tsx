import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Bomb, Save, ShieldAlert, ShieldCheck } from 'lucide-react'
import { api } from '../api'
import type { RiskSettings } from '../types'
import { Button, Card, ErrorPanel, Field, Loading, PageHeader } from '../components/UI'

export default function Risk() {
  const client = useQueryClient()
  const query = useQuery({ queryKey: ['risk-settings'], queryFn: () => api<RiskSettings>('/api/risk-settings') })
  const [form, setForm] = useState<RiskSettings | null>(null)
  useEffect(() => { if (query.data) setForm(query.data) }, [query.data])
  const save = useMutation({ mutationFn: () => api<RiskSettings>('/api/risk-settings', { method: 'PUT', body: JSON.stringify(form) }), onSuccess: () => client.invalidateQueries({ queryKey: ['risk-settings'] }) })
  const emergency = useMutation({ mutationFn: () => api<any>('/api/engine/emergency-liquidate', { method: 'POST', body: JSON.stringify({ reason: '用户从风险中心触发紧急平仓' }) }) })
  if (query.isLoading || !form) return <Loading label="正在加载风险闸门" />

  return <>
    <PageHeader eyebrow="RISK SENTINEL" title="风险中心" description="全局限制优先于策略设置。触发单日亏损或日内回撤阈值后，引擎会暂停新开仓并取消开放订单。" actions={<Button disabled={save.isPending} onClick={() => save.mutate()}><Save size={14} />保存风控</Button>} />
    {(save.error || emergency.error) && <ErrorPanel message={String((save.error || emergency.error) as Error)} />}
    <div className="three-column" style={{ marginBottom: 16 }}>
      <RiskGauge title="单股票仓位上限" value={form.max_symbol_pct} icon={<ShieldCheck size={18} />} />
      <RiskGauge title="总持仓暴露上限" value={form.max_total_exposure_pct} icon={<ShieldAlert size={18} />} />
      <RiskGauge title="日内回撤熔断" value={form.max_intraday_drawdown_pct} icon={<AlertTriangle size={18} />} />
    </div>
    <div className="dashboard-grid">
      <Card>
        <div className="form-section"><div className="form-section-title"><div><h3>全局风险参数</h3><p>所有百分比均以当前 Alpaca Paper 账户净值计算。</p></div></div>
          <div className="form-grid">
            <RiskField label="单只股票最大仓位 %" keyName="max_symbol_pct" form={form} setForm={setForm} />
            <RiskField label="总持仓最大暴露 %" keyName="max_total_exposure_pct" form={form} setForm={setForm} />
            <RiskField label="最大同时持仓数" keyName="max_positions" form={form} setForm={setForm} />
            <RiskField label="单日最大亏损 %" keyName="max_daily_loss_pct" form={form} setForm={setForm} />
            <RiskField label="日内高点最大回撤 %" keyName="max_intraday_drawdown_pct" form={form} setForm={setForm} />
            <RiskField label="行情过期阈值（秒）" keyName="stale_data_seconds" form={form} setForm={setForm} />
          </div>
        </div>
      </Card>
      <Card>
        <div className="card-header"><div><h2>紧急控制区</h2><p>只影响 Alpaca 模拟盘，不涉及实盘资金</p></div><Bomb size={18} color="#ff647c" /></div>
        <div className="card-pad">
          <div className="warning-callout">紧急平仓会先暂停交易引擎、取消全部未成交订单，然后向 Alpaca Paper 提交全部持仓的平仓请求。该操作需要浏览器二次确认。</div>
          <Button variant="danger" style={{ width: '100%', marginTop: 18 }} disabled={emergency.isPending} onClick={() => { if (window.confirm('确认取消全部模拟订单并平掉所有模拟持仓？')) emergency.mutate() }}><Bomb size={16} />{emergency.isPending ? '正在发送指令' : '紧急撤单并全部平仓'}</Button>
          {emergency.data && <div className="warning-callout" style={{ marginTop: 14, borderColor: 'rgba(66,230,164,.25)', color: '#42e6a4' }}>紧急平仓指令已经发送到 Alpaca 模拟盘。</div>}
        </div>
      </Card>
    </div>
  </>
}

function RiskGauge({ title, value, icon }: { title: string; value: number; icon: React.ReactNode }) {
  return <Card className="risk-gauge"><div className="stat-topline"><span>{title}</span><span className="stat-icon">{icon}</span></div><div className="stat-value">{value}%</div><div className="risk-bar"><span style={{ width: `${Math.min(100, value)}%` }} /></div></Card>
}
function RiskField({ label, keyName, form, setForm }: { label: string; keyName: keyof RiskSettings; form: RiskSettings; setForm: (value: RiskSettings) => void }) {
  return <Field label={label}><input type="number" value={form[keyName]} onChange={(e) => setForm({ ...form, [keyName]: Number(e.target.value) })} /></Field>
}
