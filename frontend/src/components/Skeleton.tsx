/**
 * Professional skeleton placeholder — a subtle shimmer sweep instead of a
 * spinner. The sweep is a transform (GPU-friendly) and collapses to a static
 * block under prefers-reduced-motion via the global stylesheet.
 */
export default function Skeleton({ label = 'Loading content' }: { label?: string }) {
  return (
    <div className="skeleton" role="status" aria-label={label}>
      <span className="skeleton-row" style={{ width: '38%' }} />
      <span className="skeleton-row" style={{ width: '100%' }} />
      <span className="skeleton-row" style={{ width: '92%' }} />
      <span className="skeleton-row" style={{ width: '64%' }} />
    </div>
  )
}