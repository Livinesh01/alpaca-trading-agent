import { useEffect, useState } from 'react'
import { AlertTriangle, CircleDollarSign, ShieldCheck } from 'lucide-react'
import { DEMO_RISK } from '../lib/demo'
import { usePrefersReducedMotion } from '../lib/motion'

const BARS = [
  { label: 'Exposure', value: DEMO_RISK.exposure },
  { label: 'Concentration', value: DEMO_RISK.concentration },
  { label: 'Risk usage', value: DEMO_RISK.utilization },
]

/** Show the current risk posture and controls. */
export default function RiskPosture() {
  const reduced = usePrefersReducedMotion()
  const [grown, setGrown] = useState(reduced)

  useEffect(() => {
    if (reduced) {
      setGrown(true)
      return
    }
    const id = window.setTimeout(() => setGrown(true), 180)
    return () => window.clearTimeout(id)
  }, [reduced])

  return (
    <section className="panel risk-posture" aria-label="Risk posture">
      <div className="panel-header">
        <h2>Risk posture</h2>
        <span className={grown ? 'risk-status on' : 'risk-status'}>
          <span className="risk-status-dot" /> SAFE
        </span>
      </div>
      <div className="risk-bars">
        {BARS.map((bar) => (
          <div className="risk-bar-row" key={bar.label}>
            <div className="risk-bar-head">
              <span>{bar.label}</span>
              <strong>{bar.value.toFixed(1)}%</strong>
            </div>
            <div className="risk-bar-track">
              <span style={{ width: grown ? `${bar.value}%` : '0%' }} />
            </div>
          </div>
        ))}
      </div>
      <div className="risk-list">
        <div>
          <ShieldCheck size={15} />
          <span>Kill switch</span>
          <strong className="positive">{DEMO_RISK.killSwitch}</strong>
        </div>
        <div>
          <AlertTriangle size={15} />
          <span>Stale data blocks</span>
          <strong>{DEMO_RISK.staleBlocks}</strong>
        </div>
        <div>
          <CircleDollarSign size={15} />
          <span>Daily loss limit</span>
          <strong>{DEMO_RISK.lossLimit}</strong>
        </div>
      </div>
      <p className="risk-note">risk_rules.py and the final order gate stay authoritative in every mode.</p>
    </section>
  )
}