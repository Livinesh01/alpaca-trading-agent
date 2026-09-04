import { useEffect, useState } from 'react'
import { Cpu, Database, LineChart, Lock, Play, ShieldCheck, Sparkles } from 'lucide-react'
import { PIPELINE_STAGES } from '../lib/demo'
import { usePrefersReducedMotion } from '../lib/motion'

const STAGE_ICONS = [Database, LineChart, Sparkles, Cpu, ShieldCheck, Lock, Play]

/** How long the completed pipeline remains visible. */
const HOLD_STEPS = 3
const STEP_MS = 950

/** Show the order path from LLM proposal to final approval. */
export default function Pipeline() {
  const reduced = usePrefersReducedMotion()
  const [step, setStep] = useState(0)

  useEffect(() => {
    if (reduced) return
    const id = window.setInterval(() => {
      setStep((s) => (s >= PIPELINE_STAGES.length + HOLD_STEPS ? 0 : s + 1))
    }, STEP_MS)
    return () => window.clearInterval(id)
  }, [reduced])

  const stateFor = (i: number): 'done' | 'active' | 'idle' => {
    if (reduced) return 'done' // static, fully-lit — no looping animation
    if (i < step) return 'done'
    if (i === step) return 'active'
    return 'idle'
  }

  return (
    <section className="panel pipeline-panel" aria-label="AI decision pipeline">
      <div className="panel-header pipeline-header">
        <h2>Decision pipeline</h2>
        <span className="pipeline-caption">LLM → direction · Python → sizing · risk validates · final gate executes</span>
      </div>
      <ol className="pipeline">
        {PIPELINE_STAGES.map((stage, i) => {
          const state = stateFor(i)
          const Icon = STAGE_ICONS[i] ?? Cpu
          return (
            <li key={stage.key} className={`stage ${state} auth-${stage.authority.toLowerCase()}`} tabIndex={0}>
              <div className="stage-rail">
                <span className="stage-node">
                  <Icon size={14} />
                </span>
                {i < PIPELINE_STAGES.length - 1 && (
                  <span className="stage-line">
                    <span className="stage-line-fill" />
                  </span>
                )}
              </div>
              <div className="stage-card">
                <strong>{stage.label}</strong>
                <small>{stage.authority}</small>
                <div className="stage-detail">
                  <span>{stage.purpose}</span>
                  <em>Status: {stage.status}</em>
                </div>
              </div>
            </li>
          )
        })}
      </ol>
    </section>
  )
}