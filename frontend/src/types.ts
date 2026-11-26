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
  direction?: 'long' | 'short'
  instrument?: 'stock' | 'option'
  option?: OptionConfig | null
}

export type OptionConfig = {
  right: 'call' | 'put'
  min_dte: number
  max_dte: number
  strike_mode: 'atm' | 'otm_pct' | 'itm_pct'
  strike_offset_pct: number
  min_open_interest: number
  max_spread_pct: number
  stop_loss_pct: number | null
  take_profit_pct: number | null
  close_dte: number
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

export type ConnectionState = 'unconfigured' | 'connected' | 'degraded' | 'circuit_open' | 'unknown'

export type ConnectionStatus = {
  broker?: 'alpaca' | 'ibkr'
  account_id?: string
  configured: boolean
  connected: boolean
  state?: ConnectionState
  paper: boolean
  feed: string
  source?: 'web' | 'env' | 'none'
  message: string
  consecutive_failures?: number
  last_success_at?: string | null
  last_failure_at?: string | null
  retry_at?: string | null
  last_error_category?: string | null
}

export type EngineOperationalStatus = 'active' | 'degraded' | 'circuit_open' | 'paused'

export type ExecutionIncident = {
  id: number
  symbol: string
  status: string
  reason: string
  updated_at: string
  details: {
    actual_qty?: number | null
    owned_qty?: number
    reserved_sell_qty?: number
    snapshot_started_at?: string
    positions_observed_at?: string
    orders_observed_at?: string
    snapshot_completed_at?: string
    snapshot_error?: string
    destructive?: boolean
    resolution?: string
    snapshot_attempts?: number
    open_orders?: Array<{ id: string; parent_order_id?: string; status: string; qty: string | number; filled_qty: string | number; remaining_qty?: number }> | null
  }
}

export type EngineStatus = {
  status: string
  operational_status?: EngineOperationalStatus
  operational_reason?: string
  accepting_new_orders?: boolean
  reason: string
  last_heartbeat: string | null
  enabled_strategies?: number
  paper?: boolean
  connection_state?: ConnectionState
  last_alpaca_success_at?: string | null
  next_retry_at?: string | null
  active_incidents?: string[]
  execution_incidents?: ExecutionIncident[]
  evaluation_health?: EvaluationHealth
}

export type EvaluationRow = {
  strategy_id: string
  strategy_name: string
  symbol: string
  timeframe: Timeframe
  outcome: string
  bar_timestamp: string | null
  checked_at: string
  stalled: boolean
  entry_matched?: boolean
  exit_matched?: boolean
  has_position?: boolean
  error?: string
}

export type EvaluationHealth = { checked_at: string; tracked: number; stalled: string[]; rows: EvaluationRow[] }

export type PortfolioPosition = {
  symbol: string
  qty: number
  side: string
  avg_entry_price: number
  current_price: number
  market_value: number
  cost_basis: number
  unrealized_pl: number
  unrealized_plpc: number
  change_today_pct: number
  owners: Array<{ strategy_id: string; strategy_name: string; qty: number; side?: 'long' | 'short' }>
  strategy_qty: number
  unattributed_qty: number
  instrument?: 'stock' | 'option'
  underlying?: string
  expiration?: string
  option_right?: 'call' | 'put'
  strike?: number
}

export type PortfolioOrder = {
  id: string
  parent_order_id: string | null
  symbol: string
  side: string
  type: string
  status: string
  qty: string | number | null
  filled_qty: string | number | null
  limit_price: string | number | null
  stop_price: string | number | null
  submitted_at: string | null
  strategy_name: string | null
}

export type PortfolioSnapshot = {
  as_of: string
  source: string
  paper: boolean
  status: 'ok' | 'suspect' | 'unavailable'
  errors: string[]
  checks: string[]
  market_open?: boolean
  next_open?: string | null
  next_close?: string | null
  account?: {
    equity: number
    last_equity: number | null
    cash: number
    buying_power: number
    market_value: number
    day_change: number | null
    day_change_pct: number | null
    exposure_pct: number | null
    trading_blocked: boolean
  }
  positions?: PortfolioPosition[]
  orders?: PortfolioOrder[]
}

export type CashSweepStatus = {
  state: 'disabled' | 'paused' | 'cooldown' | 'ready'
  reason: string
  until: string | null
  last_rotation_at: string | null
  min_trade_usd: number
}

export type DataAvailability = 'fresh' | 'stale' | 'unavailable' | boolean

export type DashboardData = {
  connection: ConnectionStatus
  account: Record<string, string | number | boolean> | null
  positions: Array<Record<string, string | number>> | null
  orders: Array<Record<string, string | number>> | null
  clock: Record<string, string | boolean> | null
  availability?: Partial<Record<'account' | 'positions' | 'orders' | 'clock', DataAvailability>>
  data_errors?: Partial<Record<'account' | 'positions' | 'orders' | 'clock', string>>
  snapshot_at?: string
  portfolio?: PortfolioSnapshot
  engine: EngineStatus
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
  cash_sweep_enabled: boolean
  cash_sweep_symbol: string
  cash_sweep_buffer_pct: number
  cash_sweep_paused_until: string | null
  cash_sweep_cooldown_minutes: number
  cash_sweep_min_trade_usd: number
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

export type PerformanceTrade = {
  symbol: string
  qty: number
  entry_time: string
  entry_price: number
  entry_reason: string
  exit_time: string
  exit_price: number
  exit_kind: string
  exit_label: string
  exit_reason: string
  pnl: number
  pnl_pct: number
  holding_minutes: number
  exit_order_id: string
  side?: 'long' | 'short'
}

export type StrategyPerformance = {
  strategy_id: string
  name: string
  enabled: boolean
  symbols: string[]
  realized_pnl: number
  unrealized_pnl: number
  total_pnl: number
  trades: number
  wins: number
  losses: number
  win_rate: number | null
  avg_win: number | null
  avg_loss: number | null
  profit_factor: number | null
  max_drawdown: number
  avg_holding_minutes: number | null
  exit_breakdown: Record<string, { count: number; pnl: number }>
  curve: Array<{ time: string; pnl: number }>
  trade_list: PerformanceTrade[]
  open_lots: Array<{ symbol: string; qty: number; side?: 'long' | 'short'; entry_price: number; entry_time: string; current_price: number | null; unrealized_pnl: number | null }>
  ownership_cleared_qty: number
  unmatched_sell_qty: number
  diagnosis: string[]
  first_trade_at: string | null
}

export type StrategyDraft = {
  definition: RuleDefinition
  explanation: string
  assumptions: string[]
  warnings: string[]
}

export type MarketDataInfo = { source: 'alpaca' | 'ibkr'; label: string; live: string; history: string; configured_source?: 'alpaca' | 'ibkr' }

export type AiProvider = 'openai' | 'openai_responses' | 'anthropic'

export type AiSettings = { configured: boolean; provider: AiProvider; base_url: string; model: string; updated_at: string | null }

export type WatchlistGroup = { id: number; name: string; position: number; symbols: string[] }

export type WatchQuote = {
  symbol: string
  price: number | null
  price_time: string | null
  session: 'pre' | 'regular' | 'post' | 'closed' | null
  prev_close: number | null
  change: number | null
  change_pct: number | null
  open: number | null
  high: number | null
  low: number | null
  volume_live: number | null
  prev_volume: number | null
  avg_volume_30: number | null
  high_52w: number | null
  low_52w: number | null
  history_feed: string | null
  live_feed: string | null
}

export type SymbolDetail = WatchQuote & { name: string | null; exchange: string | null; asset_class: string | null }
