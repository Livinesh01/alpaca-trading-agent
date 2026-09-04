// Frozen demo values. Only the visual scroll is animated.

import { tickerRows } from '../lib/demo'

function Track({ hidden }: { hidden?: boolean }) {
  return (
    <div className="ticker-track" aria-hidden={hidden || undefined}>
      {tickerRows.map((t) => (
        <span className="ticker-cell" key={t.symbol}>
          <strong>{t.symbol}</strong>
          <span className="ticker-price">{t.price.toFixed(2)}</span>
          <span className={t.change >= 0 ? 'ticker-change up' : 'ticker-change down'}>
            {t.change >= 0 ? '+' : ''}
            {t.change.toFixed(2)}%
          </span>
        </span>
      ))}
    </div>
  )
}

export default function Ticker() {
  return (
    <section className="ticker" aria-label="Demo market data ticker">
      <span className="ticker-label">DEMO MARKET DATA</span>
      <div className="ticker-window">
        <div className="ticker-strip">
          <Track />
          <Track hidden />
        </div>
      </div>
    </section>
  )
}