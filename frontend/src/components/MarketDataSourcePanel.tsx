import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Server } from 'lucide-react'
import { api } from '../api'
import type { MarketDataInfo } from '../types'
import { Badge, Card, ErrorPanel, Field } from './UI'

export function useMarketDataInfo() {
  return useQuery({ queryKey: ['market-data-info'], queryFn: () => api<MarketDataInfo>('/api/brokers/market-data'), staleTime: 60000 })
}

export default function MarketDataSourcePanel({ broker, configuredSource }: { broker: 'alpaca' | 'ibkr'; configuredSource: 'alpaca' | 'ibkr' }) {
  const client = useQueryClient()
  const info = useMarketDataInfo()
  const save = useMutation({
    mutationFn: (source: 'alpaca' | 'ibkr') => api<MarketDataInfo>('/api/brokers/market-data', { method: 'PUT', body: JSON.stringify({ source }) }),
    onSuccess: () => ['market-data-info', 'broker-config', 'connection', 'bars'].forEach((key) => client.invalidateQueries({ queryKey: [key] })),
  })
  return <Card style={{ marginBottom: 16 }}>
    <div className="card-header"><div><h2><Server size={16} style={{ display: 'inline', verticalAlign: 'middle', marginRight: 6 }} />行情来源</h2>
      <p>K线、报价和策略信号使用的数据；与下单券商分开。下单、持仓和账户始终走当前券商。</p></div>
      {info.data && <Badge tone="info">{info.data.label}</Badge>}</div>
    <div className="form-section">
      {broker === 'ibkr' ? <Field label="行情来源" hint="IBKR 免费账户没有美股实时行情，推荐用 Alpaca 免费账户的行情；需在下方填写 Alpaca 密钥，Alpaca 只用于读行情，不会下单。">
        <select aria-label="行情来源" value={configuredSource} disabled={save.isPending} onChange={(event) => save.mutate(event.target.value as 'alpaca' | 'ibkr')}>
          <option value="alpaca">Alpaca 免费行情（推荐）</option>
          <option value="ibkr">IBKR 行情（取决于账户订阅）</option>
        </select>
      </Field> : <p className="field-hint">当前券商为 Alpaca，行情直接使用 Alpaca。</p>}
      {info.data && <div className="warning-callout" style={{ color: '#cbd5e1' }}>盘中实时：{info.data.live}；历史与日线：{info.data.history}。</div>}
      {save.error && <ErrorPanel message={(save.error as Error).message} />}
    </div>
  </Card>
}
