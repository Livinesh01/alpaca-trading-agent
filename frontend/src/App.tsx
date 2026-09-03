import { useEffect, useState } from 'react'
import {
  Activity, BarChart3, Bot, BriefcaseBusiness, ChevronRight, Gauge, LayoutDashboard, LineChart, Menu,
  Package, Play, Radio, Search, Settings2, ShieldCheck, SlidersHorizontal, Sparkles, TerminalSquare, X,
} from 'lucide-react'
import { API_BASE_URL, ApiError, demoSymbols, getHealth, isDemoMode, resolveSource } from './api'
import ActivityTimeline from './components/ActivityTimeline'
import DecisionCard from './components/DecisionCard'
import Hero from './components/Hero'
import Pipeline from './components/Pipeline'
import PriceChart from './components/PriceChart'
import RiskPosture from './components/RiskPosture'
import Ticker from './components/Ticker'

const nav = [
  ['Overview', LayoutDashboard], ['Markets', LineChart], ['AI Agent', Bot], ['Positions', BriefcaseBusiness],
  ['Orders', Package], ['Risk Center', ShieldCheck], ['Backtest', BarChart3], ['Evaluation', Gauge],
  ['Activity', Activity], ['System', Settings2],
] as const

function App() {
  const [page, setPage] = useState('Overview')
  const [sidebar, setSidebar] = useState(true)
  const [search, setSearch] = useState('')
  const [connection, setConnection] = useState<'demo' | 'online' | 'offline'>(isDemoMode ? 'demo' : 'offline')

  useEffect(() => {
    if (!API_BASE_URL) return
    getHealth()
      .then(() => setConnection('online'))
      .catch(() => setConnection('offline'))
  }, [])

  const filteredSymbols = demoSymbols.filter((item) => item.symbol.includes(search.toUpperCase()))

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <button className="icon-button mobile-menu" aria-label="Toggle navigation" onClick={() => setSidebar(!sidebar)}>
            <Menu size={18} />
          </button>
          <div className="mark"><Sparkles size={15} /></div>
          <div>
            <strong>EPSILON</strong>
            <span>AI trading infrastructure</span>
          </div>
        </div>
        <div className="topbar-status">
          <span className={connection === 'online' ? 'status-dot live' : 'status-dot'} />
          <span>{connection === 'online' ? 'API connected' : connection === 'offline' ? 'Offline' : 'Local workspace'}</span>
          <span className="divider" />
          <span className="paper-badge"><Radio size={12} /> PAPER TRADING</span>
          {connection === 'demo' ? <span className="demo-badge">DEMO / SIMULATED</span> : connection === 'offline' ? <span className="offline-badge">OFFLINE</span> : null}
        </div>
        <div className="account-strip">
          <div>
            <span className="eyebrow">Portfolio equity</span>
            <strong>$26,180.40</strong>
          </div>
          <button className="avatar" aria-label="Account menu">EP</button>
        </div>
      </header>
      <div className="workspace">
        <aside className={sidebar ? 'sidebar open' : 'sidebar'}>
          <div className="sidebar-head">
            <span className="eyebrow">Workspace</span>
            <button className="icon-button" aria-label="Collapse navigation" onClick={() => setSidebar(false)}>
              <ChevronRight size={16} />
            </button>
          </div>
          <nav>
            {nav.map(([label, Icon]) => (
              <button key={label} className={page === label ? 'nav-item active' : 'nav-item'} onClick={() => setPage(label)}>
                <Icon size={17} />
                <span>{label}</span>
                {label === 'Activity' && <span className="nav-count">7</span>}
              </button>
            ))}
          </nav>
          <div className="sidebar-foot">
            <div className="system-chip">
              <span className="status-dot green-dot" />
              <div>
                <strong>System healthy</strong>
                <span>Last sync 12s ago</span>
              </div>
            </div>
          </div>
        </aside>
        <main className="main-content">
          <div key={page} className="page-fade">
            <div className="page-heading">
              <div>
                <span className="eyebrow">{page === 'Overview' ? 'Wednesday, September 3, 2026' : 'EPSILON workspace'}</span>
                <h1>{page}</h1>
              </div>
              <div className="heading-actions">
                <button className="secondary-button"><SlidersHorizontal size={15} /> Filters</button>
                <button className="primary-button"><Play size={14} /> Run simulation</button>
              </div>
            </div>
            {page === 'Overview' ? (API_BASE_URL ? <ApiStatePage connection={connection} /> : <Overview filteredSymbols={filteredSymbols} search={search} setSearch={setSearch} />) : <PlaceholderPage page={page} symbols={filteredSymbols} />}
          </div>
        </main>
      </div>
    </div>
  )
}

function Overview({ filteredSymbols, search, setSearch }: { filteredSymbols: typeof demoSymbols; search: string; setSearch: (value: string) => void }) {
  const [noticeOpen, setNoticeOpen] = useState(true)
  return (
    <>
      <Hero />
      <Ticker />
      <div className="metric-grid">
        <Metric label="Cash" value="$9,235.90" detail="35.3% of equity" />
        <Metric label="Buying power" value="$9,235.90" detail="Available now" />
        <Metric label="Open exposure" value="$4,648.68" detail="17.8% of equity" />
        <Metric label="Data freshness" value="12 sec" detail="All symbols current · demo" status />
      </div>
      <Pipeline />
      <div className="content-grid">
        <section className="panel market-panel">
          <PanelHeader title="Watchlist" action="Markets" />
          <div className="table-tools">
            <div className="search-field">
              <Search size={15} />
              <input aria-label="Search symbols" placeholder="Search symbols" value={search} onChange={(event) => setSearch(event.target.value)} />
            </div>
            <span className="table-meta">5 symbols · demo data</span>
          </div>
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Last</th>
                <th>Day</th>
                <th>Position</th>
                <th>AI signal</th>
              </tr>
            </thead>
            <tbody>
              {filteredSymbols.map((item) => (
                <tr key={item.symbol}>
                  <td><strong>{item.symbol}</strong><small className="cell-sub">{item.freshness}</small></td>
                  <td className="number">${item.price.toFixed(2)}</td>
                  <td className={item.change > 0 ? 'positive number' : 'negative number'}>{item.change > 0 ? '+' : ''}{item.change.toFixed(2)}%</td>
                  <td className="muted">{item.position}</td>
                  <td><Signal value={item.signal} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
        <DecisionCard />
      </div>
      <div className="content-grid">
        <PriceChart />
        <ActivityTimeline />
      </div>
      <RiskPosture />
      {noticeOpen && (
        <div className="demo-notice">
          <TerminalSquare size={16} />
          <span>
            <strong>DEMO / SIMULATED MODE</strong> This workspace uses frozen historical data and a local simulated
            executor. No Alpaca order endpoint is connected.
          </span>
          <button aria-label="Dismiss demo notice" onClick={() => setNoticeOpen(false)}>
            <X size={15} />
          </button>
        </div>
      )}
    </>
  )
}

function PlaceholderPage({ page, symbols }: { page: string; symbols: typeof demoSymbols }) {
  const copy: Record<string, [string, string]> = {
    Markets: ['Market exploration', 'Charts and OHLCV views stay read-only in demo mode.'],
    'AI Agent': ['Decision pipeline', 'Trace every signal from market data through the final order gate.'],
    Positions: ['Positions', 'Current simulated holdings and exposure.'],
    Orders: ['Order lifecycle', 'Requested, validated, gated, and simulated execution states.'],
    'Risk Center': ['Risk center', 'Deterministic controls remain authoritative.'],
    Backtest: ['Historical backtest', 'HYPOTHETICAL BACKTEST · Local simulation only.'],
    Evaluation: ['Candidate evaluation', 'HYPOTHETICAL EVALUATION RESULT · Human review required.'],
    Activity: ['Operational activity', 'Structured events, correlation IDs, and health signals.'],
    System: ['System status', 'Backend, provider, paper state, and connectivity.'],
  }
  const [title, desc] = copy[page] || [page, 'Read-only workspace']
  return (
    <section className="placeholder panel">
      <div className="placeholder-icon"><TerminalSquare size={22} /></div>
      <span className="eyebrow">{page === 'Backtest' || page === 'Evaluation' ? 'READ-ONLY · INFORMATIONAL' : 'WORKSPACE VIEW'}</span>
      <h2>{title}</h2>
      <p>{desc}</p>
      <div className="placeholder-rule" />
      {page === 'Positions' && (
        <div className="mini-table">
          {symbols.filter((item) => item.position !== '0 sh').map((item) => (
            <div key={item.symbol}>
              <strong>{item.symbol}</strong>
              <span>{item.position}</span>
              <span>${item.price.toFixed(2)}</span>
              <Signal value={item.signal} />
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

function ApiStatePage({ connection }: { connection: 'online' | 'offline' | 'demo' }) {
  const [state, setState] = useState<'loading' | 'connected' | 'unavailable' | 'unauthorized' | 'forbidden' | 'error'>('loading')
  const [account, setAccount] = useState<number | null>(null)
  const [risk, setRisk] = useState<string | null>(null)

  useEffect(() => {
    if (connection !== 'online') {
      setState(connection === 'offline' ? 'unavailable' : 'loading')
      return
    }
    const source = resolveSource()
    Promise.all([source.getAccount(), source.getRisk()])
      .then(([accountData, riskData]) => {
        setAccount(accountData.portfolio_value)
        setRisk(riskData.reason ?? (riskData.available ? 'Risk state available' : 'Risk data unavailable'))
        setState(accountData.available || riskData.available ? 'connected' : 'unavailable')
      })
      .catch((error: unknown) => {
        if (error instanceof ApiError && error.status === 401) setState('unauthorized')
        else if (error instanceof ApiError && error.status === 403) setState('forbidden')
        else setState('error')
      })
  }, [connection])

  if (state === 'loading') {
    return <section className="placeholder panel"><div className="placeholder-icon"><Radio size={22} /></div><span className="eyebrow">CONNECTING</span><h2>Loading service data</h2><p>Requesting portfolio and risk state from the configured EPSILON API.</p></section>
  }

  const title = state === 'connected' ? 'Connected paper workspace' : state === 'unauthorized' ? 'Authentication required' : state === 'forbidden' ? 'Access forbidden' : state === 'error' ? 'Backend error' : 'Data unavailable'
  const description = state === 'connected'
    ? 'Portfolio and risk state are coming from the backend. Orders remain gated and paper-only.'
    : state === 'unauthorized'
      ? 'The API rejected this request. No demo values are shown.'
      : state === 'forbidden'
        ? 'This role cannot view the requested operational state.'
        : state === 'error'
          ? 'The backend returned an unexpected response. No demo values are shown.'
          : connection === 'offline' ? 'The API is unreachable. No live values are shown.' : 'The API is reachable, but no usable account or risk data is available.'
  return (
    <section className="placeholder panel">
      <div className="placeholder-icon"><Radio size={22} /></div>
      <span className="eyebrow">{state === 'connected' ? 'CONNECTED · PAPER TRADING' : state.toUpperCase() + ' · READ-ONLY'}</span>
      <h2>{title}</h2>
      <p>{description}</p>
      {state === 'connected' && <div className="api-state-metrics"><Metric label="Portfolio equity" value={account == null ? '--' : `$${account.toFixed(2)}`} detail="Backend account service" /><Metric label="Risk state" value="AVAILABLE" detail={risk ?? 'Backend risk service'} status /></div>}
      <div className="placeholder-rule" />
      <small className="muted">Demo values are disabled when an API URL is configured.</small>
    </section>
  )
}

function Metric({ label, value, detail, status }: { label: string; value: string; detail: string; status?: boolean }) {
  return (
    <div className="metric">
      <span className="eyebrow">{label}</span>
      <strong>{status && <span className="status-dot green-dot" />}{value}</strong>
      <small>{detail}</small>
    </div>
  )
}

function PanelHeader({ title, action }: { title: string; action: string }) {
  return (
    <div className="panel-header">
      <h2>{title}</h2>
      <button className="quiet-button">{action} <ChevronRight size={14} /></button>
    </div>
  )
}

function Signal({ value }: { value: string }) {
  return <span className={`signal ${value.toLowerCase()}`}><span />{value}</span>
}

export default App
