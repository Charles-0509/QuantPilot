import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Sparkles } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import type { AiSettings, RuleDefinition, StrategyDraft } from '../types'
import { Badge, Button, Card, ErrorPanel } from './UI'

const EXAMPLES = [
  'QQQ 在 15 分钟线上，RSI14 低于 30 且收盘价高于 200 日均线时买入，RSI 回到 55 以上卖出，止损 2%，止盈 5%，每次用 5% 资金。',
  'SPY 日线突破过去 20 天最高收盘价时买入，跌破过去 10 天最低收盘价时卖出，用 ATR 2 倍止损。',
]

export default function StrategyAssistant({ current, disabled, onDraft }: {
  current: RuleDefinition | null
  disabled?: boolean
  onDraft: (definition: RuleDefinition) => void
}) {
  const [text, setText] = useState('')
  const settings = useQuery({ queryKey: ['ai-settings'], queryFn: () => api<AiSettings>('/api/ai/settings') })
  const draft = useMutation({
    mutationFn: () => api<StrategyDraft>('/api/ai/strategy-draft', { method: 'POST', body: JSON.stringify({ text, current_definition: current }) }),
    onSuccess: (result) => onDraft(result.definition),
  })
  return <Card style={{ marginBottom: 16 }}>
    <div className="form-section">
      <div className="form-section-title"><div><h3><Sparkles size={16} style={{ display: 'inline', verticalAlign: 'middle', marginRight: 6 }} />用自然语言写策略</h3>
        <p>用中文描述买卖规则，AI 会转换成下方的条件卡片。结果只是草稿：不会自动保存或启用，请检查并回测后再保存。{current ? '当前为修改模式，会在已有策略上调整。' : ''}</p></div>
        <Badge tone="info">AI DRAFT</Badge></div>
      {settings.data && !settings.data.configured ? <div className="warning-callout">尚未配置大模型服务。请到 <Link to="/settings">设置页</Link> 选择接口类型（OpenAI 兼容或 Anthropic），填写地址、模型和 API Key。</div> : <>
        <textarea rows={4} value={text} disabled={disabled || draft.isPending} onChange={(event) => setText(event.target.value)}
          placeholder={EXAMPLES[0]} style={{ width: '100%' }} />
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 8 }}>
          {EXAMPLES.map((example) => <button key={example} className="badge badge-neutral" style={{ cursor: 'pointer', border: 'none' }} onClick={() => setText(example)}>示例：{example.slice(0, 18)}…</button>)}
          <Button style={{ marginLeft: 'auto' }} disabled={disabled || draft.isPending || text.trim().length < 4} onClick={() => draft.mutate()}>
            <Sparkles size={14} />{draft.isPending ? '正在生成（约 10–60 秒）' : current ? '按描述修改策略' : '生成策略草稿'}</Button>
        </div>
      </>}
      {draft.error && <div style={{ marginTop: 12 }}><ErrorPanel message={(draft.error as Error).message} /></div>}
      {draft.data && <div className="stack" style={{ marginTop: 12 }}>
        {draft.data.explanation && <div className="warning-callout" style={{ whiteSpace: 'pre-wrap', color: '#cbd5e1' }}><strong>AI 的理解：</strong>{draft.data.explanation}</div>}
        {draft.data.assumptions.length > 0 && <div className="warning-callout"><strong>AI 替你做的假设（请确认）：</strong><ul>{draft.data.assumptions.map((item) => <li key={item}>{item}</li>)}</ul></div>}
        <div className="danger-callout"><strong>检查提示：</strong><ul>{draft.data.warnings.map((item) => <li key={item}>{item}</li>)}</ul></div>
      </div>}
    </div>
  </Card>
}
