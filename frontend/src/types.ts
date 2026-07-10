export type Timeframe = '5Min' | '15Min' | '30Min' | '1Hour' | '1Day'

export type Operand = {
  kind: 'price' | 'indicator' | 'number'
  field?: string | null
  indicator?: string | null
  params?: Record<string, number | boolean>
  value?: number | null
  offset?: number
}

export type Condition = {
  type: 'condition'
  left: Operand
  operator: '>' | '>=' | '<' | '<=' | '==' | 'crosses_above' | 'crosses_below'
  right: Operand
  label?: string
}

export type ConditionGroup = {
  type: 'group'
  op: 'AND' | 'OR'
  negate?: boolean
  children: Array<Condition | ConditionGroup>
}

export type RuleDefinition = {
  version: 1
  name: string
  description: string
  symbols: string[]
  timeframe: Timeframe
  warmup_bars: number
  schedule: { session: 'regular'; weekdays: number[] }
  entry: ConditionGroup
  exit: ConditionGroup
  position: {
    mode: 'percent_equity' | 'fixed_notional' | 'fixed_qty' | 'risk_based'
    value: number
    allow_pyramiding: boolean
    max_additions: number
  }
  order: {
    type: 'market' | 'limit'
    limit_offset_bps: number
    time_in_force: 'day' | 'gtc'
    stop_loss: PriceGuard | null
    take_profit: PriceGuard | null
    trailing_stop: { mode: 'percent' | 'price'; value: number } | null
  }
  risk: { max_symbol_pct: number; max_positions: number; cooldown_bars: number }
}

export type PriceGuard = { mode: 'percent' | 'atr'; value: number; atr_period: number }

export type Strategy = {
  id: string
  name: string
  description: string
  template_key: string | null
  is_template: boolean
  enabled: boolean
  version: number
  definition: RuleDefinition
  created_at: string
  updated_at: string
}

export type BacktestRun = {
  id: string
  strategy_id: string
  status: string
  parameters: Record<string, unknown>
  metrics: Record<string, number>
  equity_curve: Array<{ timestamp: string; equity: number }>
  benchmark_curve: Array<{ timestamp: string; equity: number }>
  trades: Array<Record<string, string | number>>
  error: string | null
  created_at: string
  completed_at: string | null
}

export type BacktestSummary = Omit<BacktestRun, 'equity_curve' | 'benchmark_curve' | 'trades'>

export type DashboardData = {
  connection: { configured: boolean; connected: boolean; paper: boolean; feed: string; message: string }
  account: Record<string, string | number | boolean>
  positions: Array<Record<string, string | number>>
  orders: Array<Record<string, string | number>>
  clock: Record<string, string | boolean>
  engine: { status: string; reason: string; last_heartbeat: string | null }
  events: Array<{ level: string; category: string; message: string; created_at: string }>
  signals: Array<{ symbol: string; action: string; price: number; reason: string; status: string; created_at: string }>
}

export type RiskSettings = {
  id: number
  max_symbol_pct: number
  max_total_exposure_pct: number
  max_positions: number
  max_daily_loss_pct: number
  max_intraday_drawdown_pct: number
  stale_data_seconds: number
}

export type ConnectionConfig = {
  configured: boolean
  paper: true
  source: 'web' | 'env' | 'none'
  api_key_hint: string | null
  feed: 'iex'
  updated_at: string | null
}

export type AuthUser = {
  id: number
  username: string
  role: 'admin' | 'user'
  is_active: boolean
  alpaca_configured: boolean
  created_at: string
  last_login_at: string | null
}

export type AuthStatus = {
  setup_required: boolean
  authenticated: boolean
  user: AuthUser | null
}

export type OAuthToken = {
  access_token: string
  token_type: 'bearer'
  expires_in: number
  scope: 'admin' | 'user'
}
