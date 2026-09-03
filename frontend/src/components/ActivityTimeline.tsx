import { Activity, BarChart3, Bot, CheckCircle2, Cpu, Play, ShieldCheck } from 'lucide-react'
import { DEMO_TIMELINE } from '../lib/demo'

const ICONS: Record<string, typeof Activity> = {
  activity: Activity,
  bars: BarChart3,
  bot: Bot,
  cpu: Cpu,
  shield: ShieldCheck,
  check: CheckCircle2,
  play: Play,
}

/**
 * One frozen simulated agent run. Entries stagger in once on mount —
 * deterministic data, deterministic timing, no looping randomness.
 */
export default function ActivityTimeline() {
  return (
    <section className="panel timeline-panel" aria-label="Simulated agent activity">
      <div className="panel-header">
        <h2>Agent run · activity</h2>
        <span className="panel-tag">DEMO EVENT STREAM</span>
      </div>
      <div className="timeline">
        {DEMO_TIMELINE.map((event, i) => {
          const Icon = ICONS[event.icon] ?? Activity
          return (
            <div className="activity-row tl-row" key={event.title} style={{ animationDelay: `${i * 110}ms` }}>
              <span className={`activity-icon ${event.tone}`}>
                <Icon size={14} />
              </span>
              <div className="tl-body">
                <span className="tl-time">{event.time} UTC</span>
                <strong>{event.title}</strong>
                <small>{event.detail}</small>
              </div>
            </div>
          )
        })}
      </div>
      <p className="timeline-foot">Simulated correlation: decision → sizing → risk PASS → final gate PASS → fill</p>
    </section>
  )
}