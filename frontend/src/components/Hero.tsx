import { ShieldCheck, Sparkles } from 'lucide-react'
import MarketBackdrop from './MarketBackdrop'

const FLOW = ['Analyze markets', 'Reason with AI', 'Size deterministically', 'Validate risk', 'Execute safely']

/**
 * Homepage hero. States the product identity and the safety contract, with
 * explicit PAPER TRADING / DEMO / SIMULATED badges — never "live" anything.
 */
export default function Hero() {
  return (
    <section className="hero-strip hero-home">
      <MarketBackdrop />
      <div className="hero-inner">
        <div className="hero-copy">
          <div className="section-kicker">
            <Sparkles size={12} /> EPSILON · AI TRADING AGENT
          </div>
          <h2>
            Guardrails first.
            <br />
            <em>Signals second.</em>
          </h2>
          <p>
            A safety-first agent that analyzes markets, reasons with AI, and hands every order to deterministic
            sizing, risk rules, and a final execution gate.
          </p>
          <div className="hero-flow" aria-label="Decision flow">
            {FLOW.map((step, i) => (
              <span className="hero-flow-step" key={step}>
                {i > 0 && (
                  <span className="hero-flow-arrow" aria-hidden="true">
                    →
                  </span>
                )}
                {step}
              </span>
            ))}
          </div>
          <div className="hero-badges">
            <span className="hero-badge paper">
              <span className="status-dot live" /> PAPER TRADING
            </span>
            <span className="hero-badge demo">DEMO / SIMULATED</span>
          </div>
        </div>
        <div className="hero-stats">
          <div>
            <span className="eyebrow">Simulated session P&amp;L</span>
            <strong>+$184.62</strong>
            <small className="positive">+0.75% · demo run</small>
          </div>
          <div>
            <span className="eyebrow">Risk status</span>
            <strong className="risk-good">
              <ShieldCheck size={17} /> Clear
            </strong>
            <small>0 blocks today</small>
          </div>
        </div>
      </div>
    </section>
  )
}