import type { ReactNode } from 'react'
import { useState } from 'react'
import Discovery from './Discovery'
import AllocationReview from './AllocationReview'

const navItems = [
  { label: 'Overview', icon: 'grid', href: '/' },
  { label: 'Discovery', icon: 'scan', href: '/discovery' },
  { label: 'Allocations', icon: 'layers' },
  { label: 'Payouts', icon: 'arrows' },
]

function Icon({ name }: { name: string }) {
  const paths: Record<string, ReactNode> = {
    grid: <><rect x="3.5" y="3.5" width="7" height="7" rx="1"/><rect x="13.5" y="3.5" width="7" height="7" rx="1"/><rect x="3.5" y="13.5" width="7" height="7" rx="1"/><rect x="13.5" y="13.5" width="7" height="7" rx="1"/></>,
    scan: <><path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3"/><path d="M7 12h10M12 7v10"/></>,
    layers: <><path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/></>,
    arrows: <><path d="M7 7h13l-3-3M17 17H4l3 3"/><path d="M20 7v5M4 17v-5"/></>,
    book: <><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v17H6.5A2.5 2.5 0 0 1 4 17.5v-12Z"/><path d="M4 16h16M8 7h7"/></>,
    external: <><path d="M14 4h6v6M20 4l-9 9"/><path d="M18 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5"/></>,
    plus: <><path d="M12 5v14M5 12h14"/></>,
    menu: <><path d="M4 7h16M4 12h16M4 17h16"/></>,
    close: <><path d="m6 6 12 12M18 6 6 18"/></>,
  }
  return <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>
}

export default function App() {
  const [menuOpen, setMenuOpen] = useState(false)
  const route = window.location.pathname.match(/^\/discovery(?:\/([^/]+))?\/?$/)
  const allocationRoute = window.location.pathname.match(/^\/allocation\/([^/]+)\/?$/)
  const isDiscovery = !!route
  const isAllocation = !!allocationRoute
  const sessionId = route?.[1]
  const allocationId = allocationRoute?.[1]
  const currentPage = isDiscovery ? 'Discovery' : isAllocation ? 'Allocations' : 'Overview'

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">Skip to content</a>
      <aside className="sidebar" aria-label="Main navigation">
        <a className="brand" href="/" aria-label="Dividend home">
          <span className="brand-mark">d</span><span>dividend<span className="brand-period">.</span></span>
        </a>
        <div className="workspace-label">WORKSPACE</div>
        <button className="mobile-menu" type="button" aria-label={menuOpen ? 'Close navigation' : 'Open navigation'} aria-expanded={menuOpen} aria-controls="primary-navigation" onClick={() => setMenuOpen(!menuOpen)}><Icon name={menuOpen ? 'close' : 'menu'}/></button>
        <nav className={`primary-nav${menuOpen ? ' menu-open' : ''}`} id="primary-navigation">
          {navItems.map(({ label, icon, href }) => (href || (label === 'Allocations' && allocationId)) ? (
            <a key={label} className={`nav-link${currentPage === label ? ' active' : ''}`} href={href || `/allocation/${allocationId}`} aria-current={currentPage === label ? 'page' : undefined} onClick={() => setMenuOpen(false)}><Icon name={icon}/><span>{label}</span></a>
          ) : (
            <span key={label} className="nav-link nav-soon" aria-disabled="true"><Icon name={icon}/><span>{label}</span><span className="soon-dot" title="Available in a later workflow step"/></span>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="sidebar-rule"/>
          <a className="nav-link docs-link" href="https://github.com/Abhinay2007/Divident/tree/main/docs" target="_blank" rel="noreferrer"><Icon name="book"/><span>Documentation</span><Icon name="external"/></a>
          <div className="account-row"><span className="account-avatar">O</span><span className="account-copy"><strong>Open source</strong><small>Community workspace</small></span><span className="account-menu" aria-hidden="true">···</span></div>
        </div>
      </aside>

      <div className="main-column">
        <header className="topbar">
          <div className="breadcrumbs"><span>Workspace</span><span className="crumb-slash">/</span><strong>{currentPage}</strong></div>
          <div className="topbar-right"><span className="sandbox-pill"><span/>Sandbox environment</span><button className="help-button" type="button" aria-label="About the sandbox">?</button></div>
        </header>
        <main id="main" className={`main-content${isDiscovery ? ' main-content-discovery' : ''}`}>
          {isDiscovery ? <Discovery sessionId={sessionId}/> : allocationId ? <AllocationReview planId={allocationId}/> : <>
          <div className="page-heading">
            <div><div className="eyebrow">OPEN-SOURCE FUNDING</div><h1>Good software deserves<br className="desktop-break"/> a sustainable future.</h1><p className="intro">See what your system depends on. Decide what to fund. Keep every dollar explainable.</p></div>
            <span className="date-stamp">DEBIAN · UBUNTU</span>
          </div>

          <section className="workflow-strip" aria-label="Funding workflow">
            <div className="workflow-step current"><span className="step-index">01</span><span><strong>Discover</strong><small>Installed packages</small></span></div>
            <span className="workflow-line" aria-hidden="true"/>
            <div className="workflow-step"><span className="step-index">02</span><span><strong>Review</strong><small>Projects &amp; allocations</small></span></div>
            <span className="workflow-line" aria-hidden="true"/>
            <div className="workflow-step"><span className="step-index">03</span><span><strong>Fund</strong><small>Sandbox payments</small></span></div>
          </section>

          <section className="start-panel" aria-labelledby="start-title">
            <div className="panel-topline"><span>GET STARTED</span><span className="panel-code">DV · 001</span></div>
            <div className="panel-content">
              <div className="panel-copy"><div className="scan-symbol"><span/><span/><span/></div><h2 id="start-title">Start with your software.</h2><p>Explore a curated Debian package set, or bring a package list from a system you manage.</p>
                <div className="panel-actions"><a className="button button-primary" href="/discovery"><Icon name="scan"/>Open package discovery</a><button className="button button-secondary" type="button" disabled title="Package upload will be available in a later milestone">Import package list <span className="coming-soon">SOON</span></button></div>
              </div>
              <div className="panel-aside"><div className="aside-label">HOW IT WORKS</div><ol className="mini-steps"><li><span>1</span><div><strong>Map dependencies</strong><small>Connect packages to upstream projects.</small></div></li><li><span>2</span><div><strong>Review the plan</strong><small>Understand every proposed allocation.</small></div></li><li><span>3</span><div><strong>Choose to fund</strong><small>Approve sandbox payments yourself.</small></div></li></ol><p className="assurance"><span className="assurance-mark">✓</span> Nothing is charged without your approval.</p></div>
            </div>
          </section>

          <div className="below-grid">
            <section className="quiet-card" aria-labelledby="activity-title"><div className="card-heading"><h2 id="activity-title">Recent activity</h2><span className="muted-label">THIS WORKSPACE</span></div><div className="empty-activity"><span className="empty-indicator"/><p>Your discovery sessions will appear here.</p></div></section>
            <section className="quiet-card note-card" aria-labelledby="trust-title"><div className="card-heading"><h2 id="trust-title">Built for clarity</h2><span className="note-icon">↗</span></div><p>Allocations are deterministic and reviewable. Payments use PayPal sandbox until you explicitly approve a plan.</p><a href="https://www.paypal.com/us/webapps/mpp/ua/useragreement-full" target="_blank" rel="noreferrer">How sandbox payments work <Icon name="external"/></a></section>
          </div>
          <footer className="page-footer"><span>Dividend <span className="brand-period">·</span> Funding the software we depend on.</span><span>Payments remain in sandbox</span></footer>
          </>}
        </main>
      </div>
    </div>
  )
}
