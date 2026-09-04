/** Show a loading placeholder with reduced-motion support. */
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