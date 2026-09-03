import { useMemo } from 'react'
import { mulberry32 } from '../lib/demo'

type Candle = {
  x: number
  w: number
  body: number
  y: number
  up: boolean
  delay: number
  duration: number
}

const TICKS = [
  { left: '21%', delay: '0s' },
  { left: '47%', delay: '2.7s' },
  { left: '72%', delay: '5.4s' },
]

/**
 * Quiet, deterministic market backdrop rendered behind the hero copy:
 * a faint quote grid, a row of drifting candlesticks, and slow price ticks.
 * Purely decorative — aria-hidden, CSS-transform only, disabled on small
 * screens via CSS and flattened for prefers-reduced-motion users.
 */
export default function MarketBackdrop() {
  const candles = useMemo<Candle[]>(() => {
    const rand = mulberry32(42)
    return Array.from({ length: 16 }, (_, i) => ({
      x: 2 + i * 6.1 + rand() * 1.8,
      w: 5 + rand() * 3,
      body: 16 + rand() * 48,
      y: 16 + rand() * 56,
      up: rand() > 0.45,
      delay: i * 0.55,
      duration: 8 + rand() * 7,
    }))
  }, [])

  return (
    <div className="market-backdrop" aria-hidden="true">
      <div className="backdrop-grid" />
      <div className="backdrop-candles">
        {candles.map((c, i) => (
          <span
            key={i}
            className={c.up ? 'backdrop-candle up' : 'backdrop-candle down'}
            style={{
              left: `${c.x}%`,
              width: `${c.w}px`,
              height: `${c.body}px`,
              top: `${c.y}%`,
              animationDelay: `${c.delay}s`,
              animationDuration: `${c.duration}s`,
            }}
          />
        ))}
      </div>
      <div className="backdrop-line" />
      {TICKS.map((t) => (
        <span key={t.left} className="backdrop-tick" style={{ left: t.left, animationDelay: t.delay }} />
      ))}
    </div>
  )
}