import type { OptionConfig, RuleDefinition } from '../types'
import { Field } from './UI'

export type TradeKind = 'long' | 'short' | 'option'

export const DEFAULT_OPTION: OptionConfig = {
  right: 'call', min_dte: 14, max_dte: 45, strike_mode: 'atm', strike_offset_pct: 0, min_open_interest: 100,
  max_spread_pct: 25, stop_loss_pct: 50, take_profit_pct: 100, close_dte: 1,
}

export function tradeKind(definition: RuleDefinition): TradeKind {
  if (definition.instrument === 'option') return 'option'
  return definition.direction === 'short' ? 'short' : 'long'
}

export const TRADE_KIND_LABEL: Record<TradeKind, string> = { long: '股票做多', short: '股票做空', option: '买入期权' }

/** Switch kind and clear settings the backend rejects for that kind. */
export function withTradeKind(definition: RuleDefinition, kind: TradeKind): RuleDefinition {
  const base = { ...definition }
  delete (base as Record<string, unknown>).paper_validation
  if (kind === 'long') return { ...base, direction: 'long', instrument: 'stock', option: null }
  if (kind === 'short') return { ...base, direction: 'short', instrument: 'stock', option: null,
    order: { ...base.order, trailing_stop: null } }
  return {
    ...base, direction: 'long', instrument: 'option', option: definition.option ?? DEFAULT_OPTION,
    order: { ...base.order, stop_loss: null, take_profit: null, trailing_stop: null, time_in_force: 'day' },
    position: base.position.mode === 'risk_based' ? { ...base.position, mode: 'percent_equity', value: 2 } : base.position,
  }
}

const KIND_HINT: Record<TradeKind, string> = {
  long: '条件成立时买入，离场条件成立时卖出。',
  short: '条件成立时卖出开空，离场条件成立时买入平空。只做易借券股票、整股；止损在上方，止盈在下方；不能用移动止损。账户净值需至少 2,000 美元。',
  option: '条件在标的K线上计算，成立时买入一张符合规则的期权合约（看涨或看跌），只买入开仓、不卖出开仓。期权报价来自免费 indicative 数据。',
}

export default function InstrumentPanel({ definition, disabled, onChange }: {
  definition: RuleDefinition
  disabled?: boolean
  onChange: (definition: RuleDefinition) => void
}) {
  const kind = tradeKind(definition)
  const option = definition.option ?? DEFAULT_OPTION
  const setOption = (patch: Partial<OptionConfig>) => onChange({ ...definition, option: { ...option, ...patch } })
  const number = (value: string) => (value === '' ? 0 : Number(value))
  return <div className="form-section">
    <div className="form-section-title"><div><h3>交易方向与品种</h3><p>{KIND_HINT[kind]}</p></div></div>
    <div className="segmented" role="radiogroup" aria-label="交易方向与品种">
      {(['long', 'short', 'option'] as TradeKind[]).map((item) => <button key={item} type="button" role="radio" aria-checked={kind === item}
        className={kind === item ? 'active' : ''} disabled={disabled} onClick={() => onChange(withTradeKind(definition, item))}>{TRADE_KIND_LABEL[item]}</button>)}
    </div>
    {kind === 'option' && <>
      <div className="form-grid-3" style={{ marginTop: 14 }}>
        <Field label="合约类型"><select aria-label="合约类型" value={option.right} disabled={disabled} onChange={(e) => setOption({ right: e.target.value as OptionConfig['right'] })}>
          <option value="call">看涨期权（Call）</option><option value="put">看跌期权（Put）</option></select></Field>
        <Field label="最短到期天数"><input aria-label="最短到期天数" type="number" min={1} value={option.min_dte} disabled={disabled} onChange={(e) => setOption({ min_dte: number(e.target.value) })} /></Field>
        <Field label="最长到期天数"><input aria-label="最长到期天数" type="number" min={1} value={option.max_dte} disabled={disabled} onChange={(e) => setOption({ max_dte: number(e.target.value) })} /></Field>
        <Field label="行权价" hint="平值取最接近现价的行权价"><select aria-label="行权价" value={option.strike_mode} disabled={disabled}
          onChange={(e) => setOption({ strike_mode: e.target.value as OptionConfig['strike_mode'], strike_offset_pct: e.target.value === 'atm' ? 0 : option.strike_offset_pct || 5 })}>
          <option value="atm">平值</option><option value="otm_pct">虚值（按现价百分比）</option><option value="itm_pct">实值（按现价百分比）</option></select></Field>
        <Field label="行权价偏移 %"><input aria-label="行权价偏移" type="number" step="0.5" value={option.strike_offset_pct} disabled={disabled || option.strike_mode === 'atm'} onChange={(e) => setOption({ strike_offset_pct: number(e.target.value) })} /></Field>
        <Field label="到期前几天平仓" hint="必须小于最短到期天数"><input aria-label="到期前几天平仓" type="number" min={0} value={option.close_dte} disabled={disabled} onChange={(e) => setOption({ close_dte: number(e.target.value) })} /></Field>
        <Field label="权利金止损 %" hint="留空表示不止损"><input aria-label="权利金止损" type="number" value={option.stop_loss_pct ?? ''} disabled={disabled} onChange={(e) => setOption({ stop_loss_pct: e.target.value === '' ? null : Number(e.target.value) })} /></Field>
        <Field label="权利金止盈 %" hint="留空表示不止盈"><input aria-label="权利金止盈" type="number" value={option.take_profit_pct ?? ''} disabled={disabled} onChange={(e) => setOption({ take_profit_pct: e.target.value === '' ? null : Number(e.target.value) })} /></Field>
        <Field label="最大买卖价差 %"><input aria-label="最大买卖价差" type="number" value={option.max_spread_pct} disabled={disabled} onChange={(e) => setOption({ max_spread_pct: number(e.target.value) })} /></Field>
        <Field label="最小持仓量（张）"><input aria-label="最小持仓量" type="number" min={0} value={option.min_open_interest} disabled={disabled} onChange={(e) => setOption({ min_open_interest: number(e.target.value) })} /></Field>
      </div>
      <p className="field-hint">仓位按权利金计算：每张合约 = 权利金 × 100。合约在每次入场时从当前上市合约中选择，同一标的同时只持有规则选出的合约。</p>
    </>}
  </div>
}
