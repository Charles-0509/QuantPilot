import { Braces, GitBranchPlus, Plus, Trash2 } from 'lucide-react'
import type { Condition, ConditionGroup, Operand } from '../types'
import { Button, Field } from './UI'

const indicatorDefaults: Record<string, Record<string, number | boolean>> = {
  SMA: { period: 20 }, EMA: { period: 20 }, RSI: { period: 14 },
  MACD: { fast: 12, slow: 26, signal: 9 }, BOLLINGER: { period: 20, std: 2 },
  ATR: { period: 14 }, ROC: { period: 12 }, HIGHEST: { period: 20, exclude_current: true },
  LOWEST: { period: 20, exclude_current: true }, VOLUME_SMA: { period: 20, multiplier: 1.5 }, DEVIATION: { period: 20 },
}

const indicators = Object.keys(indicatorDefaults)
const operators = ['>', '>=', '<', '<=', '==', 'crosses_above', 'crosses_below'] as const

function defaultCondition(): Condition {
  return {
    type: 'condition',
    left: { kind: 'price', field: 'close', offset: 0 },
    operator: '>',
    right: { kind: 'indicator', indicator: 'SMA', field: 'value', params: { period: 20 }, offset: 0 },
    label: '价格高于 SMA20',
  }
}

export default function RuleBuilder({ value, onChange, title }: { value: ConditionGroup; onChange: (group: ConditionGroup) => void; title: string }) {
  return <div className="condition-builder">
    <div className="form-section-title"><div><h3>{title}</h3><p>条件只读取已经完成的K线；上穿和下穿会同时检查前一根K线。</p></div></div>
    <GroupEditor value={value} onChange={onChange} depth={0} />
  </div>
}

function GroupEditor({ value, onChange, depth, onRemove }: { value: ConditionGroup; onChange: (group: ConditionGroup) => void; depth: number; onRemove?: () => void }) {
  const updateChild = (index: number, child: Condition | ConditionGroup) => {
    const children = [...value.children]; children[index] = child; onChange({ ...value, children })
  }
  const removeChild = (index: number) => onChange({ ...value, children: value.children.filter((_, i) => i !== index) })
  return <div className={`condition-group ${depth ? 'nested' : ''}`}>
    <div className="condition-group-header">
      <div className="condition-group-actions"><Braces size={15} color="#a775ff" /><select value={value.op} onChange={(e) => onChange({ ...value, op: e.target.value as 'AND' | 'OR' })}><option value="AND">全部满足 AND</option><option value="OR">任一满足 OR</option></select>
        <button className={`badge ${value.negate ? 'badge-warning' : 'badge-neutral'}`} onClick={() => onChange({ ...value, negate: !value.negate })}>{value.negate ? '已取反 NOT' : '取反'}</button></div>
      <div className="condition-group-actions">
        <Button type="button" variant="ghost" onClick={() => onChange({ ...value, children: [...value.children, defaultCondition()] })}><Plus size={13} />条件</Button>
        {depth < 3 && <Button type="button" variant="ghost" onClick={() => onChange({ ...value, children: [...value.children, { type: 'group', op: 'AND', negate: false, children: [defaultCondition()] }] })}><GitBranchPlus size={13} />分组</Button>}
        {onRemove && <Button type="button" variant="danger" className="icon-button" onClick={onRemove}><Trash2 size={14} /></Button>}
      </div>
    </div>
    {value.children.length === 0 && <div className="warning-callout">这个分组没有条件，因此永远不会触发。</div>}
    {value.children.map((child, index) => child.type === 'group'
      ? <GroupEditor key={index} value={child} depth={depth + 1} onChange={(next) => updateChild(index, next)} onRemove={() => removeChild(index)} />
      : <ConditionEditor key={index} value={child} onChange={(next) => updateChild(index, next)} onRemove={() => removeChild(index)} />)}
  </div>
}

function ConditionEditor({ value, onChange, onRemove }: { value: Condition; onChange: (condition: Condition) => void; onRemove: () => void }) {
  return <div className="condition-row">
    <OperandEditor value={value.left} onChange={(left) => onChange({ ...value, left })} />
    <Field label="比较关系"><select value={value.operator} onChange={(e) => onChange({ ...value, operator: e.target.value as Condition['operator'] })}>{operators.map((operator) => <option key={operator} value={operator}>{operatorLabel(operator)}</option>)}</select></Field>
    <OperandEditor value={value.right} onChange={(right) => onChange({ ...value, right })} />
    <Button type="button" variant="danger" className="icon-button" onClick={onRemove}><Trash2 size={14} /></Button>
    <div style={{ gridColumn: '1 / -1' }}><input value={value.label || ''} placeholder="条件说明，例如：价格突破20周期高点" onChange={(e) => onChange({ ...value, label: e.target.value })} /></div>
  </div>
}

function OperandEditor({ value, onChange }: { value: Operand; onChange: (operand: Operand) => void }) {
  const kind = value.kind
  const params = value.params || {}
  const indicator = value.indicator || 'SMA'
  const setKind = (next: Operand['kind']) => {
    if (next === 'number') onChange({ kind: 'number', value: 0, offset: 0 })
    else if (next === 'price') onChange({ kind: 'price', field: 'close', offset: 0 })
    else onChange({ kind: 'indicator', indicator: 'SMA', field: 'value', params: { period: 20 }, offset: 0 })
  }
  const setParam = (key: string, raw: string) => onChange({ ...value, params: { ...params, [key]: Number(raw) } })
  return <div className="operand-editor">
    <Field label="类型"><select value={kind} onChange={(e) => setKind(e.target.value as Operand['kind'])}><option value="price">价格字段</option><option value="indicator">技术指标</option><option value="number">常数</option></select></Field>
    {kind === 'price' && <Field label="字段"><select value={value.field || 'close'} onChange={(e) => onChange({ ...value, field: e.target.value })}><option value="open">开盘价</option><option value="high">最高价</option><option value="low">最低价</option><option value="close">收盘价</option><option value="volume">成交量</option></select></Field>}
    {kind === 'number' && <Field label="数值"><input type="number" value={value.value ?? 0} onChange={(e) => onChange({ ...value, value: Number(e.target.value) })} /></Field>}
    {kind === 'indicator' && <>
      <Field label="指标"><select value={indicator} onChange={(e) => { const next = e.target.value; onChange({ ...value, indicator: next, field: 'value', params: indicatorDefaults[next] }) }}>{indicators.map((name) => <option key={name}>{name}</option>)}</select></Field>
      <div className="param-input">
        {Object.entries(params).filter(([, val]) => typeof val === 'number').map(([key, val]) => <Field label={paramLabel(key)} key={key}><input type="number" step={key === 'multiplier' || key === 'std' ? '0.1' : '1'} value={String(val)} onChange={(e) => setParam(key, e.target.value)} /></Field>)}
        {indicator === 'MACD' && <Field label="输出"><select value={value.field || 'macd'} onChange={(e) => onChange({ ...value, field: e.target.value })}><option value="macd">MACD线</option><option value="signal">信号线</option><option value="histogram">柱状图</option></select></Field>}
        {indicator === 'BOLLINGER' && <Field label="输出"><select value={value.field || 'middle'} onChange={(e) => onChange({ ...value, field: e.target.value })}><option value="upper">上轨</option><option value="middle">中轨</option><option value="lower">下轨</option></select></Field>}
      </div>
    </>}
  </div>
}

function operatorLabel(value: string) {
  return ({ '>': '大于', '>=': '大于等于', '<': '小于', '<=': '小于等于', '==': '等于', crosses_above: '上穿', crosses_below: '下穿' } as Record<string, string>)[value]
}
function paramLabel(value: string) {
  return ({ period: '周期', fast: '快线', slow: '慢线', signal: '信号', std: '标准差', multiplier: '倍数' } as Record<string, string>)[value] || value
}
