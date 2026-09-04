// Frozen demo fixtures. No network, random values, or runtime dates.

import { demoSymbols } from '../api'

/** Small deterministic random generator for demo data. */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a |= 0
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

// Ticker data.

/** Frozen quote rows for the demo ticker (mirrors api.ts demoSymbols). */
export const tickerRows = demoSymbols.map(({ symbol, price, change }) => ({ symbol, price, change }))

// Price chart data.

export type Timeframe = '1D' | '1W' | '1M'
export type ChartPoint = { label: string; price: number }

const AAPL_DEMO_CLOSE = 241.35 // api.ts DEMO_PRICES.AAPL

function intradayLabel(i: number, n: number): string {
  const minutes = Math.round((390 * i) / (n - 1)) // 09:30 → 16:00
  const h = 9 + Math.floor((30 + minutes) / 60)
  const m = (30 + minutes) % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
}

function weekLabel(i: number, n: number): string {
  const days = ['MON', 'TUE', 'WED', 'THU', 'FRI']
  return days[Math.min(days.length - 1, Math.floor((i / (n - 1)) * days.length))]
}

function monthLabel(i: number, n: number): string {
  return `W${Math.min(4, Math.floor((i / (n - 1)) * 4)) + 1}`
}

const TF_CONFIG: Record<Timeframe, { points: number; seed: number; bias: number; labeler: (i: number, n: number) => string }> = {
  '1D': { points: 48, seed: 7, bias: 0.48, labeler: intradayLabel },
  '1W': { points: 60, seed: 11, bias: 0.465, labeler: weekLabel },
  '1M': { points: 72, seed: 23, bias: 0.46, labeler: monthLabel },
}

/** Build a stable price series ending at the demo close. */
export function demoSeries(timeframe: Timeframe): ChartPoint[] {
  const cfg = TF_CONFIG[timeframe]
  const rand = mulberry32(cfg.seed)
  const stepSize = AAPL_DEMO_CLOSE * 0.006
  const prices: number[] = [AAPL_DEMO_CLOSE]
  for (let i = 1; i < cfg.points; i++) {
    const drift = (rand() - cfg.bias) * stepSize // bias < 0.5 → uptrend into the close
    prices.push(prices[i - 1] - drift)
  }
  prices.reverse()
  return prices.map((price, i) => ({ label: cfg.labeler(i, cfg.points), price: Math.round(price * 100) / 100 }))
}

// Pipeline data.

export type PipelineStage = {
  key: string
  label: string
  authority: 'PYTHON' | 'LLM' | 'SYSTEM'
  purpose: string
  status: string
}

/** Ordered decision pipeline used by the demo. */
export const PIPELINE_STAGES: PipelineStage[] = [
  { key: 'market-data', label: 'MARKET DATA', authority: 'PYTHON', purpose: 'Fresh OHLCV snapshot for every watchlist symbol.', status: 'Fresh fixture' },
  { key: 'signals', label: 'TECHNICAL SIGNALS', authority: 'PYTHON', purpose: 'Trend, momentum, volatility and RSI states computed in code.', status: 'Calculated' },
  { key: 'ai-decision', label: 'AI DECISION', authority: 'LLM', purpose: 'Direction and confidence only — the model never sizes orders.', status: 'Direction only' },
  { key: 'sizing', label: 'PYTHON SIZING', authority: 'PYTHON', purpose: 'Deterministic quantity from fixed sizing rules — never the LLM.', status: 'Deterministic' },
  { key: 'risk', label: 'RISK CHECK', authority: 'PYTHON', purpose: 'risk_rules.py validates caps, exposure and stale data.', status: 'PASS' },
  { key: 'gate', label: 'FINAL GATE', authority: 'PYTHON', purpose: 'Last invariant check before anything may be executed.', status: 'PASS' },
  { key: 'execution', label: 'PAPER EXECUTION', authority: 'SYSTEM', purpose: 'Simulated fill in paper mode. No Alpaca order endpoint is connected.', status: 'Simulated' },
]

// Activity timeline data.

export type TimelineTone = 'green' | 'blue' | 'amber' | 'slate'
export type TimelineEvent = { time: string; title: string; detail: string; tone: TimelineTone; icon: string }

/** One frozen simulated agent run. */
export const DEMO_TIMELINE: TimelineEvent[] = [
  { time: '09:31:02', title: 'Market snapshot received', detail: '5 symbols · fresh within 12 seconds', tone: 'slate', icon: 'activity' },
  { time: '09:31:02', title: 'Technical signals calculated', detail: 'Trend, momentum, volatility, RSI states', tone: 'blue', icon: 'bars' },
  { time: '09:31:03', title: 'AI decision generated', detail: 'MSFT · BUY · 78% confidence', tone: 'amber', icon: 'bot' },
  { time: '09:31:03', title: 'Python sizing calculated', detail: 'Deterministic quantity · 4 shares', tone: 'blue', icon: 'cpu' },
  { time: '09:31:03', title: 'Risk check passed', detail: 'Order cap and position headroom clear', tone: 'green', icon: 'shield' },
  { time: '09:31:04', title: 'Final gate passed', detail: '7 invariants verified', tone: 'green', icon: 'check' },
  { time: '09:31:04', title: 'Paper execution simulated', detail: 'MSFT · 4 shares · simulated fill', tone: 'green', icon: 'play' },
]

// Risk posture data.

/** Demo risk values derived from the demo account. */
export const DEMO_RISK = {
  exposure: 17.8,
  concentration: 11.1,
  utilization: 33.5,
  killSwitch: 'OFF',
  staleBlocks: 0,
  lossLimit: '$500',
} as const