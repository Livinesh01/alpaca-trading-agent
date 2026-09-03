// EPSILON API client.
//
// Three explicit wiring states:
//   DEMO   — VITE_API_BASE_URL is empty: the UI shows clearly-labeled simulated
//            data (frozen samples). No Alpaca, no network.
//   ONLINE — VITE_API_BASE_URL is set and the health probe succeeds: all data
//            comes from the FastAPI server (which proxies the risk-guard path).
//   OFFLINE— VITE_API_BASE_URL is set but the server is unreachable: explicit
//            offline state. We NEVER silently fall back to demo data.
// The browser never calls Alpaca directly and never sees credentials.

export type Connection = 'demo' | 'online' | 'offline'

export type ApiEnvelope<T> = { data: T; request_id: string; mode?: string }
export type Pagination = { page: number; page_size: number; total: number }

export type Account = {
  available: boolean
  portfolio_value: number | null
  equity: number | null
  cash: number | null
  buying_power: number | null
  daily_pnl: number | null
  currency: string | null
  account_status: string | null
  trading_mode: string | null
  paper_trading: boolean
  as_of: string | null
  reason: string | null
}

export type Position = {
  symbol: string
  quantity: number | null
  average_entry: number | null
  current_price: number | null
  market_value: number | null
  unrealized_pnl: number | null
  unrealized_pnl_percent: number | null
  exposure: number | null
  portfolio_percent: number | null
}

export type Order = {
  order_id: string
  symbol: string
  side: string
  quantity: number | null
  order_type: string
  status: string
  display_state: string
  submitted_at: string | null
  filled_at: string | null
  decision_id: string | null
  run_id: string | null
  correlation: string | null
}

export type Signals = {
  symbol?: string
  trend?: string | null
  momentum_state?: string | null
  volatility_state?: string | null
  rsi_state?: string | null
  close?: number | null
  bar_count?: number
  insufficient_data?: boolean
  [key: string]: unknown
}

export type Bar = {
  timestamp: string | null
  open: number | null
  high: number | null
  low: number | null
  close: number | null
  volume: number | null
}

export type MarketData = {
  symbol: string
  available: boolean
  is_fresh: boolean
  age_seconds: number | null
  data_timestamp: string | null
  current_price: number | null
  bars: Bar[]
  signals: Signals
  latest_trade: { price: number | null; timestamp: string | null; age_seconds: number | null } | null
  source: string
  timeframe: string | null
  as_of: string | null
  reason: string | null
}

export type Decision = {
  decision_id: string
  run_id: string | null
  timestamp: string | null
  symbol: string
  action: string
  confidence: number
  position_size: number
  thesis: string
  entry_reason: string
  decision_price: number | null
  signals?: Signals
  model?: string | null
  provider?: string | null
}

export type ReplayStage = {
  stage: string
  status: string
  authority: string
  timestamp: string | null
  metadata?: Record<string, unknown>
}

export type Replay = {
  decision: Decision
  stages: ReplayStage[]
  llm_authority: { authority: string; action: string; confidence: number; thesis: string; entry_reason: string }
  python_authority: {
    authority: string
    position_size_parsed: number
    deterministic_quantity: number | null
    risk_allowed: boolean | null
    risk_reason: string | null
    final_gate: boolean | null
    decision_price: number | null
  }
  execution: Record<string, unknown> | null
  outcome: Record<string, unknown> | null
  read_only: boolean
  label: string
}

export type RiskState = {
  available: boolean
  daily_pnl: number | null
  gross_exposure: number | null
  position_concentration: number | null
  risk_utilization: number | null
  max_position_notional_usd: number | null
  max_order_notional_usd: number | null
  stale_data_blocks: number
  risk_rejections: number
  final_gate_rejections: number
  kill_switch: boolean
  trading_mode: string | null
  paper_trading: boolean
  authoritative_source: string
  computed_at: string | null
  reason: string | null
}

export type ActivityEvent = {
  timestamp: string | null
  age_seconds: number | null
  event_type: string | null
  severity: string
  run_id: string | null
  decision_id: string | null
  execution_id: string | null
  outcome_id: string | null
  symbol: string | null
  fields: Record<string, unknown>
}

export type BacktestRecord = {
  backtest_id: string
  symbol: string
  generated_at: string | null
  label: string
  read_only: boolean
  actual_production_execution: boolean
  config: Record<string, unknown>
  metrics: Record<string, number | null>
  equity_curve: { index: number; equity: number }[]
  trade_count: number | null
}

export type EvaluationRecord = {
  evaluation_id: string
  dataset: Record<string, unknown>
  candidates: Record<string, unknown>[]
  recommendation: string | null
  generated_at: string | null
  label: string
  human_review_required: boolean
  auto_deployed: boolean
}

export type SystemInfo = {
  backend: string
  provider: string | null
  paper_trading: boolean
  dry_run: boolean
  kill_switch: boolean
  health: Record<string, unknown>
}

export type Health = {
  status: string
  paper_trading: boolean
  kill_switch: boolean
  llm_provider: string | null
  market_data: string
  market_data_fresh: boolean
  last_success: string | number | null
  version: string
  authentication: string
}

export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
export const isDemoMode = API_BASE_URL === ''

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function apiGet<T>(path: string, params?: Record<string, string | number | boolean | undefined>, headers: HeadersInit = {}): Promise<ApiEnvelope<T>> {
  if (!API_BASE_URL) throw new ApiError(0, 'not_configured', 'VITE_API_BASE_URL is not configured')
  const url = new URL(`${API_BASE_URL}${path}`)
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined) url.searchParams.set(key, String(value))
    }
  }
  const response = await fetch(url, { headers: { Accept: 'application/json', ...headers } })
  if (!response.ok) {
    let code = 'http_error'
    let message = `API request failed (${response.status})`
    try {
      const body = (await response.json()) as { code?: string; message?: string }
      if (body.code) code = body.code
      if (body.message) message = body.message
    } catch {
      // non-JSON error body — keep generic message
    }
    throw new ApiError(response.status, code, message)
  }
  return response.json() as Promise<ApiEnvelope<T>>
}

function collection<T>(path: string, params?: Record<string, string | number | boolean | undefined>) {
  // Normalize the envelope: SentinelSource promises non-optional reason/pagination.
  return apiGet<{ items: T[]; available: boolean; reason?: string | null; pagination?: Pagination }>(path, params).then(
    (r): { items: T[]; available: boolean; reason: string | null; pagination: Pagination } => ({
      items: r.data.items,
      available: r.data.available,
      reason: r.data.reason ?? null,
      pagination: r.data.pagination ?? { page: 1, page_size: Math.max(r.data.items.length, 1), total: r.data.items.length },
    }),
  )
}

export type SentinelSource = {
  mode: 'demo' | 'api'
  getAccount(): Promise<Account>
  getPositions(): Promise<{ items: Position[]; available: boolean; reason: string | null; pagination: Pagination }>
  getOrders(): Promise<{ items: Order[]; available: boolean; reason: string | null; pagination: Pagination }>
  getMarketData(symbol: string): Promise<MarketData>
  getSignals(symbol: string): Promise<{ symbol: string; signals: Signals; is_fresh: boolean; current_price: number | null; age_seconds: number | null; available: boolean; reason: string | null }>
  getDecisions(params?: Record<string, string | number | boolean>): Promise<{ items: Decision[]; pagination: Pagination }>
  getReplay(id: string): Promise<Replay>
  getRisk(): Promise<RiskState>
  getActivity(params?: Record<string, string | number | boolean>): Promise<{ items: ActivityEvent[]; pagination: Pagination }>
  getBacktests(): Promise<{ items: BacktestRecord[]; pagination: Pagination }>
  getBacktest(id: string): Promise<BacktestRecord | null>
  getEvaluations(): Promise<{ items: EvaluationRecord[]; pagination: Pagination }>
  getSystem(): Promise<SystemInfo>
  getHealth(): Promise<Health>
}

export function createApiSource(): SentinelSource {
  return {
    mode: 'api',
    getAccount: () => apiGet<Account>('/api/v1/account').then((r) => r.data),
    getPositions: () => collection<Position>('/api/v1/positions'),
    getOrders: () => collection<Order>('/api/v1/orders'),
    getMarketData: (symbol) => apiGet<MarketData>(`/api/v1/market-data/${encodeURIComponent(symbol)}`).then((r) => r.data),
    getSignals: (symbol) =>
      apiGet<{ symbol: string; signals: Signals; is_fresh: boolean; current_price: number | null; age_seconds: number | null; available: boolean; reason: string | null }>(`/api/v1/market-data/${encodeURIComponent(symbol)}/signals`).then((r) => r.data),
    getDecisions: (params) => collection<Decision>('/api/v1/decisions', params as Record<string, string | boolean | undefined>),
    getReplay: async (id) => (await apiGet<Replay>(`/api/v1/decisions/${encodeURIComponent(id)}/replay`)).data,
    getRisk: () => apiGet<RiskState>('/api/v1/risk').then((r) => r.data),
    getActivity: (params) => collection<ActivityEvent>('/api/v1/activity', params as Record<string, string | boolean | undefined>),
    getBacktests: () => collection<BacktestRecord>('/api/v1/backtests'),
    getBacktest: async (id) => {
      try {
        return (await apiGet<BacktestRecord>(`/api/v1/backtests/${encodeURIComponent(id)}`)).data
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null
        throw error
      }
    },
    getEvaluations: () => collection<EvaluationRecord>('/api/v1/evaluations'),
    getSystem: () => apiGet<SystemInfo>('/api/v1/system').then((r) => r.data),
    getHealth: () => apiGet<Health>('/api/v1/health').then((r) => r.data),
  }
}

export const DEMO_LABEL = 'DEMO / SIMULATED'
export const WATCHLIST = ['AAPL', 'MSFT', 'NVDA', 'SPY', 'TSLA']

// Demo source: deterministic simulated data, labeled everywhere it appears.
// Never mixed with API data — API mode that fails surfaces OFFLINE rather
// than silently falling back to these values.

const DEMO_TS = '2026-01-15T14:31:02Z'
const DEMO_BARS = 30

const DEMO_PRICES: Record<string, number> = {
  AAPL: 241.35,
  MSFT: 438.12,
  NVDA: 138.4,
  SPY: 604.55,
  TSLA: 252.9,
}

function demoSignals(symbol: string): Signals {
  const table: Record<string, Signals> = {
    AAPL: { trend: 'uptrend', momentum_state: 'positive', volatility_state: 'normal', rsi_state: 'neutral', close: DEMO_PRICES.AAPL, bar_count: DEMO_BARS, insufficient_data: false },
    MSFT: { trend: 'uptrend', momentum_state: 'positive', volatility_state: 'normal', rsi_state: 'neutral', close: DEMO_PRICES.MSFT, bar_count: DEMO_BARS, insufficient_data: false },
    NVDA: { trend: 'downtrend', momentum_state: 'negative', volatility_state: 'elevated', rsi_state: 'oversold', close: DEMO_PRICES.NVDA, bar_count: DEMO_BARS, insufficient_data: false },
    SPY: { trend: 'flat', momentum_state: 'neutral', volatility_state: 'normal', rsi_state: 'neutral', close: DEMO_PRICES.SPY, bar_count: DEMO_BARS, insufficient_data: false },
    TSLA: { trend: 'flat', momentum_state: 'neutral', volatility_state: 'elevated', rsi_state: 'overbought', close: DEMO_PRICES.TSLA, bar_count: DEMO_BARS, insufficient_data: false },
  }
  return table[symbol] ?? { trend: 'unknown', momentum_state: 'neutral', volatility_state: 'normal', rsi_state: 'neutral', close: DEMO_PRICES.AAPL, bar_count: 0, insufficient_data: true }
}

function demoBars(symbol: string): Bar[] {
  // Deterministic wobble around the symbol price (no Math.random).
  const base = DEMO_PRICES[symbol] ?? DEMO_PRICES.AAPL
  return Array.from({ length: DEMO_BARS }, (_, i) => {
    const wobble = Math.sin(i * 1.7 + symbol.length) * base * 0.004
    const close = Math.round((base + wobble) * 100) / 100
    return {
      timestamp: `2026-01-15T14:0${Math.floor(i / 10)}:${String(i % 60).padStart(2, '0')}Z`,
      open: close,
      high: Math.round(close * 1.003 * 100) / 100,
      low: Math.round(close * 0.997 * 100) / 100,
      close,
      volume: 100000 + i * 137,
    }
  })
}

const demoDecisions: Decision[] = [
  { decision_id: 'demo-decision-msft', run_id: 'demo-run-1', timestamp: DEMO_TS, symbol: 'MSFT', action: 'BUY', confidence: 0.78, position_size: 0, thesis: 'Uptrend with positive momentum, neutral RSI, normal volatility.', entry_reason: 'Gates aligned for a long entry.', decision_price: DEMO_PRICES.MSFT, signals: demoSignals('MSFT'), model: 'demo-model', provider: DEMO_LABEL },
  { decision_id: 'demo-decision-aapl', run_id: 'demo-run-1', timestamp: DEMO_TS, symbol: 'AAPL', action: 'HOLD', confidence: 0.42, position_size: 0, thesis: 'Existing position, mixed short-term evidence.', entry_reason: 'No fresh gate alignment.', decision_price: DEMO_PRICES.AAPL, signals: demoSignals('AAPL'), model: 'demo-model', provider: DEMO_LABEL },
  { decision_id: 'demo-decision-nvda', run_id: 'demo-run-1', timestamp: DEMO_TS, symbol: 'NVDA', action: 'HOLD', confidence: 0.35, position_size: 0, thesis: 'Elevated volatility and downtrend — evidence insufficient.', entry_reason: 'Wait for volatility to normalize.', decision_price: DEMO_PRICES.NVDA, signals: demoSignals('NVDA'), model: 'demo-model', provider: DEMO_LABEL },
]

const demoRisk: RiskState = {
  available: true,
  daily_pnl: 184.2,
  gross_exposure: 4648.68,
  position_concentration: 11.06,
  risk_utilization: 33.5,
  max_position_notional_usd: 2000,
  max_order_notional_usd: 1000,
  stale_data_blocks: 0,
  risk_rejections: 1,
  final_gate_rejections: 0,
  kill_switch: false,
  trading_mode: 'paper',
  paper_trading: true,
  authoritative_source: `${DEMO_LABEL} — production values come from /api/v1/risk (risk_rules.py)`,
  computed_at: DEMO_TS,
  reason: DEMO_LABEL,
}

const demoActivity: ActivityEvent[] = [
  { timestamp: '2026-01-15T14:31:00Z', age_seconds: 62, event_type: 'RUN_STARTED', severity: 'info', run_id: 'demo-run-1', decision_id: null, execution_id: null, outcome_id: null, symbol: null, fields: { backend: 'decision_loop' } },
  { timestamp: '2026-01-15T14:31:02Z', age_seconds: 60, event_type: 'DECISION_RECORDED', severity: 'info', run_id: 'demo-run-1', decision_id: 'demo-decision-msft', execution_id: null, outcome_id: null, symbol: 'MSFT', fields: { action: 'BUY', confidence: 0.78 } },
  { timestamp: '2026-01-15T14:31:03Z', age_seconds: 59, event_type: 'RISK_REJECTED', severity: 'warning', run_id: 'demo-run-0', decision_id: 'demo-decision-nvda', execution_id: null, outcome_id: null, symbol: 'NVDA', fields: { reason: 'elevated volatility cap' } },
  { timestamp: '2026-01-15T14:31:03Z', age_seconds: 59, event_type: 'ORDER_SUBMITTED', severity: 'info', run_id: 'demo-run-1', decision_id: 'demo-decision-msft', execution_id: 'demo-order-1', outcome_id: null, symbol: 'MSFT', fields: { quantity: 4, mode: 'paper (demo)' } },
  { timestamp: '2026-01-15T14:31:04Z', age_seconds: 58, event_type: 'ORDER_FILLED', severity: 'info', run_id: 'demo-run-1', decision_id: 'demo-decision-msft', execution_id: 'demo-order-1', outcome_id: 'demo-outcome-1', symbol: 'MSFT', fields: { filled_qty: 4 } },
]

const demoBacktests: BacktestRecord[] = [
  {
    backtest_id: 'demo-backtest-aapl-90d',
    symbol: 'AAPL',
    generated_at: DEMO_TS,
    label: 'HYPOTHETICAL BACKTEST',
    read_only: true,
    actual_production_execution: false,
    config: { strategy: 'sma_rsi_gates', initial_capital: 25000, transaction_costs_bps: 2, slippage_bps: 1, bars: 90 },
    metrics: { total_return: 0.043, max_drawdown: -0.021, sharpe: 1.12, win_rate: 0.58, trade_count: 12, transaction_costs: 5.0, slippage: 2.5, buy_and_hold_return: 0.031, baseline_return: 0.031 },
    equity_curve: Array.from({ length: 30 }, (_, i) => ({
      index: i,
      equity: Math.round((25000 * (1 + (0.043 * i) / 29 + Math.sin(i * 0.9) * 0.002)) * 100) / 100,
    })),
    trade_count: 12,
  },
]

const demoEvaluations: EvaluationRecord[] = [
  {
    evaluation_id: 'demo-eval-001',
    dataset: { cases: 8, market_data: 'deterministic fixtures' },
    candidates: [
      { model: 'deepseek-v4-pro-0813', provider: 'nvidia', total_return: 0.038, max_drawdown: -0.019, safety_score: 0.97, contract_compliance: 1.0 },
      { model: 'featherless-default', provider: 'featherless', total_return: 0.031, max_drawdown: -0.024, safety_score: 0.95, contract_compliance: 1.0 },
    ],
    recommendation: 'No auto-deployment — human review required',
    generated_at: DEMO_TS,
    label: 'HYPOTHETICAL EVALUATION RESULT',
    human_review_required: true,
    auto_deployed: false,
  },
]

const demoSystem: SystemInfo = {
  backend: DEMO_LABEL,
  provider: 'demo',
  paper_trading: true,
  dry_run: true,
  kill_switch: false,
  health: { status: 'demo', mode: DEMO_LABEL },
}

const demoHealth: Health = {
  status: 'demo',
  paper_trading: true,
  kill_switch: false,
  llm_provider: 'demo',
  market_data: DEMO_LABEL,
  market_data_fresh: true,
  last_success: DEMO_TS,
  version: 'demo',
  authentication: 'demo (no authentication in demo mode)',
}

export function createDemoSource(): SentinelSource {
  const account: Account = {
    available: true,
    portfolio_value: 26180.4,
    equity: 26180.4,
    cash: 9235.9,
    buying_power: 9235.9,
    daily_pnl: 184.2,
    currency: 'USD',
    account_status: 'ACTIVE',
    trading_mode: 'paper',
    paper_trading: true,
    as_of: DEMO_TS,
    reason: DEMO_LABEL,
  }
  const positions: Position[] = [
    { symbol: 'AAPL', quantity: 12, average_entry: 229.4, current_price: DEMO_PRICES.AAPL, market_value: 2896.2, unrealized_pnl: 143.4, unrealized_pnl_percent: 5.21, exposure: 2896.2, portfolio_percent: 11.06 },
    { symbol: 'MSFT', quantity: 4, average_entry: 445.1, current_price: DEMO_PRICES.MSFT, market_value: 1752.48, unrealized_pnl: -27.92, unrealized_pnl_percent: -1.57, exposure: 1752.48, portfolio_percent: 6.69 },
  ]
  const orders: Order[] = [
    { order_id: 'demo-order-1', symbol: 'MSFT', side: 'buy', quantity: 4, order_type: 'market', status: 'filled', display_state: 'FILLED', submitted_at: '2026-01-15T14:31:03Z', filled_at: '2026-01-15T14:31:04Z', decision_id: 'demo-decision-msft', run_id: 'demo-run-1', correlation: 'decision → risk PASS → final gate PASS → submitted → filled' },
    { order_id: 'demo-order-2', symbol: 'NVDA', side: 'buy', quantity: 10, order_type: 'market', status: 'rejected', display_state: 'RISK_REJECTED', submitted_at: '2026-01-14T19:02:11Z', filled_at: null, decision_id: 'demo-decision-nvda', run_id: 'demo-run-0', correlation: 'decision → risk REJECT (elevated volatility cap) → never submitted' },
  ]
  const paginated = <T,>(items: T[]): { items: T[]; pagination: Pagination } => ({
    items,
    pagination: { page: 1, page_size: Math.max(items.length, 1), total: items.length },
  })
  return {
    mode: 'demo',
    getAccount: async () => account,
    getPositions: async () => ({ ...paginated(positions), available: true, reason: DEMO_LABEL }),
    getOrders: async () => ({ ...paginated(orders), available: true, reason: DEMO_LABEL }),
    getMarketData: async (symbol) => {
      const upper = symbol.toUpperCase()
      const price = DEMO_PRICES[upper] ?? DEMO_PRICES.AAPL
      return {
        symbol: upper,
        available: true,
        is_fresh: true,
        age_seconds: 2,
        data_timestamp: DEMO_TS,
        current_price: price,
        bars: demoBars(upper),
        signals: demoSignals(upper),
        latest_trade: { price, timestamp: DEMO_TS, age_seconds: 2 },
        source: DEMO_LABEL,
        timeframe: '1Day',
        as_of: DEMO_TS,
        reason: DEMO_LABEL,
      }
    },
    getSignals: async (symbol) => {
      const upper = symbol.toUpperCase()
      return { symbol: upper, signals: demoSignals(upper), is_fresh: true, current_price: DEMO_PRICES[upper] ?? DEMO_PRICES.AAPL, age_seconds: 2, available: true, reason: DEMO_LABEL }
    },
    getDecisions: async () => paginated(demoDecisions),
    getReplay: (id) => {
      const decision = demoDecisions.find((d) => d.decision_id === id)
      if (!decision) throw new ApiError(404, 'unknown_decision', `Unknown demo decision: ${id}`)
      const filled = decision.decision_id === 'demo-decision-msft'
      const stages: ReplayStage[] = [
        { stage: 'RUN', status: 'complete', authority: 'PYTHON', timestamp: decision.timestamp, metadata: { run_id: decision.run_id } },
        { stage: 'MARKET_DATA', status: 'fresh', authority: 'PYTHON', timestamp: decision.timestamp, metadata: { source: DEMO_LABEL, age_seconds: 2 } },
        { stage: 'TECHNICAL_SIGNALS', status: 'calculated', authority: 'PYTHON', timestamp: decision.timestamp, metadata: decision.signals ?? {} },
        { stage: 'LLM_DECISION', status: decision.action, authority: 'LLM', timestamp: decision.timestamp, metadata: { confidence: decision.confidence } },
        { stage: 'SCHEMA_VALIDATION', status: 'pass', authority: 'PYTHON', timestamp: decision.timestamp, metadata: { strict: true } },
        { stage: 'PYTHON_SIZING', status: 'deterministic', authority: 'PYTHON', timestamp: decision.timestamp, metadata: { quantity: filled ? 4 : 0 } },
        { stage: 'RISK_CHECK', status: 'pass', authority: 'PYTHON', timestamp: decision.timestamp, metadata: { source: 'risk_rules.py' } },
        { stage: 'FINAL_GATE', status: 'pass', authority: 'PYTHON', timestamp: decision.timestamp, metadata: { invariants: 7 } },
        { stage: 'EXECUTION', status: filled ? 'submitted' : 'none', authority: 'SYSTEM', timestamp: decision.timestamp, metadata: { mode: 'paper (demo)' } },
      ]
      return Promise.resolve({
        decision,
        stages,
        llm_authority: { authority: 'LLM — decides direction only', action: decision.action, confidence: decision.confidence, thesis: decision.thesis, entry_reason: decision.entry_reason },
        python_authority: { authority: 'PYTHON — sizing, risk, final approval', position_size_parsed: decision.position_size, deterministic_quantity: filled ? 4 : 0, risk_allowed: true, risk_reason: 'all rules passed', final_gate: true, decision_price: decision.decision_price },
        execution: filled ? { order_id: 'demo-order-1', status: 'filled', mode: 'paper (demo)' } : null,
        outcome: filled ? { outcome_id: 'demo-outcome-1', filled_qty: 4 } : null,
        read_only: true,
        label: `REPLAY — ${DEMO_LABEL} (read-only)`,
      })
    },
    getRisk: async () => demoRisk,
    getActivity: async () => paginated(demoActivity),
    getBacktests: async () => paginated(demoBacktests),
    getBacktest: async (id) => demoBacktests.find((b) => b.backtest_id === id) ?? null,
    getEvaluations: async () => paginated(demoEvaluations),
    getSystem: async () => demoSystem,
    getHealth: async () => demoHealth,
  }
}

/**
 * Single entry point for the app: demo source when no VITE_API_BASE_URL is
 * configured, the real FastAPI source otherwise. API mode that fails
 * surfaces OFFLINE — it never silently falls back to demo data.
 */
export function resolveSource(): SentinelSource {
  return isDemoMode ? createDemoSource() : createApiSource()
}

// ---------------------------------------------------------------------------
// Demo watchlist + standalone health probe (used by the app shell).
// ---------------------------------------------------------------------------

export type DemoSymbolRow = {
  symbol: string
  price: number
  change: number
  freshness: string
  position: string
  signal: string
}

/** Frozen watchlist rows for the demo workspace — mirrors DEMO_PRICES. */
export const demoSymbols: DemoSymbolRow[] = [
  { symbol: 'AAPL', price: DEMO_PRICES.AAPL, change: 1.28, freshness: 'fresh · 12s', position: '12 sh', signal: 'BUY' },
  { symbol: 'MSFT', price: DEMO_PRICES.MSFT, change: 0.72, freshness: 'fresh · 12s', position: '4 sh', signal: 'BUY' },
  { symbol: 'NVDA', price: DEMO_PRICES.NVDA, change: -0.48, freshness: 'fresh · 13s', position: '0 sh', signal: 'HOLD' },
  { symbol: 'SPY', price: DEMO_PRICES.SPY, change: 0.21, freshness: 'fresh · 12s', position: '0 sh', signal: 'HOLD' },
  { symbol: 'TSLA', price: DEMO_PRICES.TSLA, change: -1.15, freshness: 'fresh · 14s', position: '0 sh', signal: 'HOLD' },
]

/** Standalone health probe: demo source in demo mode, the API otherwise. */
export function getHealth(): Promise<Health> {
  return isDemoMode ? Promise.resolve(demoHealth) : createApiSource().getHealth()
}