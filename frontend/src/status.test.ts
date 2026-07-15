import { describe, expect, it } from 'vitest'
import { connectionPresentation, dataAvailable, enginePresentation, errorCategoryLabel } from './status'

describe('connectionPresentation', () => {
  it.each([
    ['unconfigured', '未配置 Alpaca', 'warning', 'unconfigured'],
    ['connected', '模拟盘已连接', 'success', 'online'],
    ['degraded', 'Alpaca 连接不稳定', 'warning', 'degraded'],
    ['circuit_open', 'Alpaca 连接保护中', 'danger', 'circuit-open'],
    ['unknown', 'Alpaca 状态未知', 'neutral', 'unknown'],
  ] as const)('maps %s to an explicit visual state', (state, label, tone, dotClass) => {
    expect(connectionPresentation({ state, configured: state !== 'unconfigured', connected: state === 'connected' })).toMatchObject({
      state,
      label,
      tone,
      dotClass,
    })
  })

  it('keeps compatibility with the previous boolean response', () => {
    expect(connectionPresentation({ configured: true, connected: false }).state).toBe('degraded')
    expect(connectionPresentation({ configured: false, connected: false }).state).toBe('unconfigured')
  })

  it('distinguishes a failed status request from an unconfigured account', () => {
    expect(connectionPresentation(undefined, true)).toMatchObject({ label: '状态读取失败', tone: 'danger' })
  })
})

describe('enginePresentation', () => {
  it('only marks an operational engine as active', () => {
    expect(enginePresentation({ status: 'running', operational_status: 'active', accepting_new_orders: true })).toMatchObject({
      label: '引擎执行中',
      strategyLabel: 'ACTIVE',
      strategyTone: 'success',
      active: true,
    })
  })

  it.each([
    ['degraded', '引擎已开启 · 连接不稳定', 'warning'],
    ['circuit_open', '引擎已开启 · 等待自动恢复', 'danger'],
  ] as const)('does not render %s as green ACTIVE', (operationalStatus, label, tone) => {
    expect(enginePresentation({ status: 'running', operational_status: operationalStatus, accepting_new_orders: false })).toMatchObject({
      label,
      tone,
      strategyLabel: 'ENABLED / WAITING',
      active: false,
    })
  })
})

describe('status helpers', () => {
  it('treats explicit unavailable data as unavailable even when fallback data exists', () => {
    expect(dataAvailable('unavailable', true)).toBe(false)
    expect(dataAvailable('stale', false)).toBe(true)
  })

  it('renders known error categories in Chinese', () => {
    expect(errorCategoryLabel('tls')).toBe('TLS 安全连接')
    expect(errorCategoryLabel('authentication')).toBe('身份验证')
    expect(errorCategoryLabel('connection')).toBe('网络连接')
    expect(errorCategoryLabel('upstream_5xx')).toBe('Alpaca 服务')
    expect(errorCategoryLabel('custom')).toBe('custom')
  })
})
