import { formatShanghaiDateTime } from './time'

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

const FIELD_LABELS: Record<string, string> = {
  'definition.name': '策略名称',
  'definition.description': '策略说明',
  'definition.symbols': '股票池',
  'definition.timeframe': 'K线周期',
  'definition.warmup_bars': '预热K线数',
  'definition.risk.max_symbol_pct': '单股票上限 %',
  'definition.risk.max_positions': '最大持仓只数',
  'definition.risk.cooldown_bars': '冷却K线数',
  'definition.position.value': '仓位数值',
  'definition.order.limit_offset_bps': '限价偏移基点',
  'max_symbol_pct': '单股票持仓上限 %',
  'max_total_exposure_pct': '总持仓上限 %',
  'max_positions': '最大持仓只数',
  'max_daily_loss_pct': '单日亏损限制 %',
  'max_intraday_drawdown_pct': '盘中最大回撤限制 %',
  'stale_data_seconds': '行情数据过期阈值',
  'cash_sweep_symbol': '现金管理理财标的',
  'cash_sweep_buffer_pct': '现金保留缓冲比例',
  'username': '用户名',
  'password': '密码',
  'old_password': '原密码',
  'new_password': '新密码',
}

function translateValidationMsg(msg: string): string {
  if (!msg) return msg
  let res = msg
  res = res.replace(/Input should be less than or equal to (\d+(?:\.\d+)?)/g, '数值必须小于或等于 $1')
  res = res.replace(/Input should be greater than or equal to (\d+(?:\.\d+)?)/g, '数值必须大于或等于 $1')
  res = res.replace(/Input should be greater than (\d+(?:\.\d+)?)/g, '数值必须大于 $1')
  res = res.replace(/Input should be less than (\d+(?:\.\d+)?)/g, '数值必须小于 $1')
  res = res.replace(/Field required/g, '不能为空')
  res = res.replace(/String should have at least (\d+) characters/g, '字符长度至少为 $1 位')
  res = res.replace(/String should have at most (\d+) characters/g, '字符长度最多为 $1 位')
  res = res.replace(/List should have at least (\d+) items/g, '列表至少需要 $1 项')
  res = res.replace(/List should have at most (\d+) items/g, '列表最多允许 $1 项')
  res = res.replace(/Input should be a valid integer, unable to parse string as an integer/g, '请输入有效的整数')
  res = res.replace(/Input should be a valid number, unable to parse string as a number/g, '请输入有效的数字')
  res = res.replace(/Input should be a valid boolean, unable to parse string as a boolean/g, '请输入有效的布尔值')
  return res
}

export function formatApiErrorDetail(payload: any, fallback: string): string {
  if (!payload) return fallback
  const detail = payload.detail ?? payload.message ?? payload.error
  if (typeof detail === 'string' && detail.trim()) return translateValidationMsg(detail)
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === 'string') return translateValidationMsg(item)
        if (item && typeof item === 'object') {
          const rawLoc = Array.isArray(item.loc)
            ? item.loc.filter((l: any) => l !== 'body').join('.')
            : ''
          const locLabel = FIELD_LABELS[rawLoc] || rawLoc
          const rawMsg = item.msg || item.message || JSON.stringify(item)
          const msg = translateValidationMsg(rawMsg)
          return locLabel ? `${locLabel}: ${msg}` : msg
        }
        return String(item)
      })
      .filter(Boolean)
    if (messages.length) return messages.join('；')
  }
  if (detail && typeof detail === 'object') {
    try {
      return JSON.stringify(detail)
    } catch {
      return fallback
    }
  }
  return fallback
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method || 'GET').toUpperCase()
  const csrf = readCookie('quantpilot_csrf')
  const response = await fetch(path, {
    ...init,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(csrf && !['GET', 'HEAD', 'OPTIONS'].includes(method) ? { 'X-CSRF-Token': csrf } : {}),
      ...(init?.headers || {}),
    },
  })
  if (!response.ok) {
    let message = `请求失败 (${response.status})`
    try {
      const payload = await response.json()
      message = formatApiErrorDetail(payload, message)
    } catch {
      // Keep the fallback message.
    }
    if (response.status === 401 && !path.startsWith('/api/auth/')) {
      window.dispatchEvent(new CustomEvent('quantpilot:unauthorized'))
    }
    throw new ApiError(message, response.status)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export async function apiForm<T>(path: string, values: Record<string, string>): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams(values),
  })
  if (!response.ok) {
    let message = `请求失败 (${response.status})`
    try {
      const payload = await response.json()
      message = formatApiErrorDetail(payload, message)
    } catch {
      // Keep the fallback message.
    }
    throw new ApiError(message, response.status)
  }
  return response.json() as Promise<T>
}

function readCookie(name: string) {
  const prefix = `${name}=`
  const item = document.cookie.split('; ').find((value) => value.startsWith(prefix))
  return item ? decodeURIComponent(item.slice(prefix.length)) : ''
}

export function money(value: unknown, currency = 'USD') {
  const number = Number(value || 0)
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency,
    maximumFractionDigits: 2,
  }).format(number)
}

export function number(value: unknown, digits = 2) {
  return Number(value || 0).toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

export function formatTime(value?: string | null) {
  if (!value) return '—'
  return formatShanghaiDateTime(value)
}
