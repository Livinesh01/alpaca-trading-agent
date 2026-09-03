import { useEffect, useState } from 'react'
import { Bot, Check, ShieldCheck } from 'lucide-react'
import { cancelRaf, raf, usePrefersReducedMotion } from '../lib/motion'

// Deterministic demo decision (api.ts demo-decision-msft). Values are frozen —
// the card animates how they appear, never what they are.
const CONFIDENCE = 78
const COUNT_MS = 1100

export default function DecisionCard() {
  const reduced = usePrefersReducedMotion()
  const [confidence, setConfidence] = useState(reduced ? CONFIDENCE : 0)

  useEffect(() => {
    if (reduced) {
      setConfidence(CONFIDENCE)
      return
    }
    let handle = 0
    const start = performance.now()
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / COUNT_MS)
      const eased = 1 - Math.pow(1 - t, 3)
      setConfidence(Math.round(eased * CONFIDENCE))
      if (t < 1) handle = raf(tick)
    }
    handle = raf(tick)
    return () => cancelRaf(handle)
  }, [reduced])

  return (
    <section className="panel decision-panel" aria-label="Latest AI decision">
      <div className="panel-header">
        <h2>Latest AI decision</h2>
        <span className="panel-tag">DEMO</span>
      </div>
      <div className="decision-symbol">
        <div>
          <span className="eyebrow">MSFT · 09:41:23 UTC · simulated run</span>
          <h3>
            <span className="decision-action">BUY</span>
            <span className="confidence">{confidence}%</span>
          </h3>
        </div>
        <div className="decision-orbit">
          <Bot size={24} />
        </div>
      </div>
      <div className="confidence-bar" role="img" aria-label={`Confidence ${CONFIDENCE} percent`}>
        <span style={{ width: `${confidence}%` }} />
      </div>
      <p className="decision-copy">
        Momentum remains positive while volatility stays within the configured risk envelope.
      </p>
      <div className="decision-facts">
        <div className="fact reveal r1">
          <span className="eyebrow">Python position size</span>
          <strong>4 shares</strong>
          <small>Deterministic — computed in Python, never by the model</small>
        </div>
        <div className="fact reveal r2">
          <span className="eyebrow">Risk</span>
          <strong className="gate-pass">
            <ShieldCheck size={14} /> PASS
          </strong>
          <small>Order cap · exposure · stale data</small>
        </div>
        <div className="fact reveal r3">
          <span className="eyebrow">Final gate</span>
          <strong className="gate-pass">
            <Check size={14} /> PASS
          </strong>
          <small>7 invariants verified before execution</small>
        </div>
      </div>
    </section>
  )
}