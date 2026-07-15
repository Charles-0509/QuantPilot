import type { ConnectionState, ConnectionStatus, DataAvailability, EngineOperationalStatus, EngineStatus } from './types'

export type StatusTone = 'success' | 'warning' | 'danger' | 'info' | 'neutral'

export type ConnectionPresentation = {
  state: ConnectionState
  label: string
  tone: StatusTone
  dotClass: string
}

export function connectionState(status?: Partial<ConnectionStatus>): ConnectionState {
  if (status?.state) return status.state
  if (!status) return 'unknown'
  if (!status.configured) return 'unconfigured'
  return status.connected ? 'connected' : 'degraded'
}

export function connectionPresentation(
  status?: Partial<ConnectionStatus>,
  requestFailed = false,
): ConnectionPresentation {
  if (requestFailed) {
    return { state: 'unknown', label: '状态读取失败', tone: 'danger', dotClass: 'unknown' }
  }
  const state = connectionState(status)
  if (state === 'connected') {
    return { state, label: '模拟盘已连接', tone: 'success', dotClass: 'online' }
  }
  if (state === 'degraded') {
    return { state, label: 'Alpaca 连接不稳定', tone: 'warning', dotClass: 'degraded' }
  }
  if (state === 'circuit_open') {
    return { state, label: 'Alpaca 连接保护中', tone: 'danger', dotClass: 'circuit-open' }
  }
  if (state === 'unconfigured') {
    return { state, label: '未配置 Alpaca', tone: 'warning', dotClass: 'unconfigured' }
  }
  return { state: 'unknown', label: 'Alpaca 状态未知', tone: 'neutral', dotClass: 'unknown' }
}

export type EnginePresentation = {
  operationalStatus: EngineOperationalStatus
  label: string
  title: string
  tone: StatusTone
  strategyLabel: string
  strategyTone: StatusTone
  active: boolean
}

export function enginePresentation(engine?: Partial<EngineStatus>): EnginePresentation {
  let operationalStatus = engine?.operational_status
  if (!operationalStatus) {
    if (engine?.status !== 'running') operationalStatus = 'paused'
    else if (engine.accepting_new_orders === false) operationalStatus = 'degraded'
    else operationalStatus = 'active'
  }

  if (operationalStatus === 'active') {
    return {
      operationalStatus,
      label: '引擎执行中',
      title: '策略反应堆正在运行',
      tone: 'success',
      strategyLabel: 'ACTIVE',
      strategyTone: 'success',
      active: true,
    }
  }
  if (operationalStatus === 'degraded') {
    return {
      operationalStatus,
      label: '引擎已开启 · 连接不稳定',
      title: '策略反应堆等待连接稳定',
      tone: 'warning',
      strategyLabel: 'ENABLED / WAITING',
      strategyTone: 'warning',
      active: false,
    }
  }
  if (operationalStatus === 'circuit_open') {
    return {
      operationalStatus,
      label: '引擎已开启 · 等待自动恢复',
      title: '连接保护已开启，策略执行暂缓',
      tone: 'danger',
      strategyLabel: 'ENABLED / WAITING',
      strategyTone: 'danger',
      active: false,
    }
  }
  return {
    operationalStatus: 'paused',
    label: '引擎已暂停',
    title: '策略反应堆处于安全暂停',
    tone: 'warning',
    strategyLabel: 'ENABLED / PAUSED',
    strategyTone: 'neutral',
    active: false,
  }
}

export function dataAvailable(value: DataAvailability | undefined, fallback: boolean): boolean {
  if (value === false || value === 'unavailable') return false
  if (value === true || value === 'fresh' || value === 'stale') return true
  return fallback
}

export function dataStale(value: DataAvailability | undefined): boolean {
  return value === 'stale'
}

export function errorCategoryLabel(value?: string | null): string {
  const labels: Record<string, string> = {
    auth: '身份验证',
    authentication: '身份验证',
    network: '网络连接',
    connection: '网络连接',
    rate_limit: '请求限流',
    timeout: '连接超时',
    tls: 'TLS 安全连接',
    upstream: 'Alpaca 服务',
    upstream_5xx: 'Alpaca 服务',
    api_rejection: 'Alpaca API 拒绝',
    unknown: '未知错误',
  }
  if (!value) return '—'
  return labels[value.toLowerCase()] || value
}
