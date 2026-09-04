import { useEffect, useMemo, useState, type MouseEvent } from 'react'
import { demoSeries, type Timeframe } from '../lib/demo'
import { usePrefersReducedMotion } from '../lib/motion'
import Skeleton from './Skeleton'

const W = 560
const H = 232
const PAD_L = 10
const PAD_R = 48
const PAD_T = 14
const PAD_B = 26

const TIMEFRAMES: Timeframe[] = ['1D', '1W', '1M']

/** Show a deterministic demo price chart. */
export default function PriceChart() {
  const [tf, setTf] = useState<Timeframe>('1D')
  const [hover, setHover] = useState<number | null>(null)
  const [ready, setReady] = useState(false)
  const reduced = usePrefersReducedMotion()

  useEffect(() => {
    const id = window.setTimeout(() => setReady(true), 420)
    return () => window.clearTimeout(id)
  }, [])

  const points = useMemo(() => demoSeries(tf), [tf])
  const prices = points.map((p) => p.price)
  const min = Math.min(...prices)
  const max = Math.max(...prices)
  const span = max - min || 1

  const x = (i: number) => PAD_L + (i / (points.length - 1)) * (W - PAD_L - PAD_R)
  const y = (p: number) => PAD_T + (1 - (p - min) / span) * (H - PAD_T - PAD_B)

  const line = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(p.price).toFixed(1)}`).join(' ')
  const area = `${line} L${x(points.length - 1).toFixed(1)},${(H - PAD_B).toFixed(1)} L${PAD_L},${(H - PAD_B).toFixed(1)} Z`

  const gridPrices = [0, 1, 2, 3].map((g) => min + (span * g) / 3)
  const xLabels = [0, Math.floor(points.length / 3), Math.floor((2 * points.length) / 3), points.length - 1].map((i) => ({
    i,
    label: points[i].label,
  }))

  const onMove = (event: MouseEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    if (rect.width === 0) return
    const vx = ((event.clientX - rect.left) / rect.width) * W
    const ratio = (vx - PAD_L) / (W - PAD_L - PAD_R)
    const i = Math.round(ratio * (points.length - 1))
    setHover(Math.max(0, Math.min(points.length - 1, i)))
  }

  const pick = (next: Timeframe) => {
    if (next === tf) return
    setTf(next)
    setHover(null)
  }

  return (
    <section className="panel chart-panel" aria-label="Demo price chart">
      <div className="panel-header chart-header">
        <div>
          <h2>AAPL · demo series</h2>
          <small className="chart-sub">Frozen fixture — not real-time market data</small>
        </div>
        <div className="tf-controls" role="group" aria-label="Timeframe">
          {TIMEFRAMES.map((t) => (
            <button key={t} className={t === tf ? 'tf-button active' : 'tf-button'} onClick={() => pick(t)} aria-pressed={t === tf}>
              {t}
            </button>
          ))}
        </div>
      </div>
      <div className="chart-frame">
        {ready ? (
          <>
            <svg viewBox={`0 0 ${W} ${H}`} className="chart-svg" onMouseMove={onMove} onMouseLeave={() => setHover(null)} aria-hidden="true">
              <defs>
                <linearGradient id="chartFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0" stopColor="#14957f" stopOpacity="0.26" />
                  <stop offset="1" stopColor="#14957f" stopOpacity="0" />
                </linearGradient>
              </defs>
              {gridPrices.map((gp, gi) => (
                <g key={gi}>
                  <line className="chart-grid" x1={PAD_L} x2={W - PAD_R} y1={y(gp)} y2={y(gp)} />
                  <text className="chart-label" x={W - PAD_R + 6} y={y(gp) + 3}>
                    {gp.toFixed(2)}
                  </text>
                </g>
              ))}
              {xLabels.map(({ i, label }) => (
                <text
                  key={i}
                  className="chart-label"
                  x={x(i)}
                  y={H - 8}
                  textAnchor={i === 0 ? 'start' : i === points.length - 1 ? 'end' : 'middle'}
                >
                  {label}
                </text>
              ))}
              <path key={`area-${tf}`} className="chart-area" d={area} fill="url(#chartFill)" />
              <path key={`line-${tf}`} className={reduced ? 'chart-line' : 'chart-line draw'} d={line} />
              {hover !== null && (
                <g>
                  <line className="chart-crosshair" x1={x(hover)} x2={x(hover)} y1={PAD_T} y2={H - PAD_B} />
                  <circle className="chart-dot" cx={x(hover)} cy={y(points[hover].price)} r={3.5} />
                </g>
              )}
            </svg>
            {hover !== null && (
              <div
                className="chart-tooltip"
                style={{
                  left: `${Math.min(90, Math.max(10, (x(hover) / W) * 100))}%`,
                  top: `${Math.max(14, (y(points[hover].price) / H) * 100)}%`,
                }}
              >
                <strong>${points[hover].price.toFixed(2)}</strong>
                <span>
                  {points[hover].label} · demo
                </span>
              </div>
            )}
          </>
        ) : (
          <Skeleton label="Loading demo chart" />
        )}
      </div>
    </section>
  )
}