import type { ReactNode } from 'react'
import { useRef, useState } from 'react'
import Discovery from './Discovery'
import AllocationReview from './AllocationReview'
import FundScreen from './FundScreen'
import PayoutsScreen from './Payouts'
import WorkspaceActivity from './WorkspaceActivity'
import MaintainerClaim from './MaintainerClaim'
import { useMutation, useQuery } from '@tanstack/react-query'
import { ApiError, createSyntheticSandboxDemo, createUploadSession, getConfig } from './api'
import { planTokenKey } from './planAccess.js'

const navItems = [
  { label: 'Overview', icon: 'grid', href: '/' },
  { label: 'Discovery', icon: 'scan', href: '/discovery' },
]

function Icon({ name }: { name: string }) {
  const paths: Record<string, ReactNode> = {
    grid: <><rect x="3.5" y="3.5" width="7" height="7" rx="1"/><rect x="13.5" y="3.5" width="7" height="7" rx="1"/><rect x="3.5" y="13.5" width="7" height="7" rx="1"/><rect x="13.5" y="13.5" width="7" height="7" rx="1"/></>,
    scan: <><path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3"/><path d="M7 12h10M12 7v10"/></>,
    layers: <><path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/></>,
    arrows: <><path d="M7 7h13l-3-3M17 17H4l3 3"/><path d="M20 7v5M4 17v-5"/></>,
    wallet: <><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 9h18M16 14h2"/></>,
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
  const uploadInput = useRef<HTMLInputElement>(null)
  const config = useQuery({ queryKey: ['config'], queryFn: getConfig })
  const upload = useMutation({
    mutationFn: async (file: File) => {
      if (file.size > 1_000_000) throw new Error('Scan files must be 1 MB or smaller.')
      let payload: unknown
      try { payload = JSON.parse(await file.text()) }
      catch { throw new Error('Choose a valid scan.json file.') }
      return createUploadSession(payload)
    },
    onSuccess: ({ id }) => window.location.assign(`/discovery/${id}`),
  })
  const syntheticDemo = useMutation({
    mutationFn: createSyntheticSandboxDemo,
    onSuccess: ({ id, action_token }) => {
      localStorage.setItem(planTokenKey(id), action_token)
      sessionStorage.setItem(planTokenKey(id), action_token)
      window.location.assign(`/allocation/${id}`)
    },
  })
  const route = window.location.pathname.match(/^\/discovery(?:\/([^/]+))?\/?$/)
  const allocationRoute = window.location.pathname.match(/^\/allocation\/([^/]+)\/?$/)
  const fundRoute = window.location.pathname.match(/^\/fund\/([^/]+)\/?$/)
  const payoutsRoute = window.location.pathname.match(/^\/payouts\/([^/]+)\/?$/)
  const maintainerClaimRoute = window.location.pathname === '/claim' || window.location.pathname === '/claim/'
  const isDiscovery = !!route
  const isAllocation = !!allocationRoute
  const sessionId = route?.[1]
  const allocationId = allocationRoute?.[1]
  const fundId = fundRoute?.[1]
  const payoutsId = payoutsRoute?.[1]
  const planId = allocationId || fundId || payoutsId
  const isFund = !!fundId
  const isPayouts = !!payoutsId
  const currentPage = maintainerClaimRoute ? 'Maintainer claim' : isDiscovery ? 'Package discovery' : isAllocation ? 'Allocation review' : isFund ? 'Funding' : isPayouts ? 'Payouts' : 'Overview'

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
          {navItems.map(({ label, icon, href }) => {
            const active = label === 'Overview' ? !isDiscovery && !planId && !maintainerClaimRoute : isDiscovery
            return <a key={label} className={`nav-link${active ? ' active' : ''}`} href={href} aria-current={active ? 'page' : undefined} onClick={() => setMenuOpen(false)}><Icon name={icon}/><span>{label}</span></a>
          })}
          {planId && <div className="nav-context" aria-label="Current plan">
            <span className="nav-context-label">CURRENT PLAN</span>
            <a className={`nav-link${isAllocation ? ' active' : ''}`} href={`/allocation/${planId}`} aria-current={isAllocation ? 'page' : undefined} onClick={() => setMenuOpen(false)}><Icon name="layers"/><span>Allocation review</span></a>
            <a className={`nav-link${isFund ? ' active' : ''}`} href={`/fund/${planId}`} aria-current={isFund ? 'page' : undefined} onClick={() => setMenuOpen(false)}><Icon name="wallet"/><span>Funding</span></a>
            <a className={`nav-link${isPayouts ? ' active' : ''}`} href={`/payouts/${planId}`} aria-current={isPayouts ? 'page' : undefined} onClick={() => setMenuOpen(false)}><Icon name="arrows"/><span>Payouts</span></a>
          </div>}
        </nav>
        <div className="sidebar-bottom">
          <div className="sidebar-rule"/>
          <a className="nav-link docs-link" href="https://github.com/Abhinay2007/Divident/tree/main/docs" target="_blank" rel="noreferrer"><Icon name="book"/><span>Documentation</span><Icon name="external"/></a>
          <p className="sidebar-note">Every plan is reviewed before payment.</p>
        </div>
      </aside>

      <div className="main-column">
        <header className="topbar">
          <div className="breadcrumbs"><span>Workspace</span><span className="crumb-slash">/</span><strong>{currentPage}</strong></div>
          <div className="topbar-right"><span className="sandbox-pill" role="status"><span/>{config.data ? `${config.data.payments_mode === 'fake' ? 'Fake provider' : 'PayPal'} · ${config.data.paypal_env}` : config.isError ? 'Payment environment unavailable' : 'Checking payment environment…'}</span>{config.isError && <span role="alert">{config.error instanceof ApiError && config.error.requestId ? `Request ID ${config.error.requestId}` : 'Configuration unavailable'} <button className="text-button" type="button" onClick={() => { void config.refetch() }}>Retry</button></span>}</div>
        </header>
        <main id="main" className={`main-content${isDiscovery ? ' main-content-discovery' : ''}`}>
          {maintainerClaimRoute ? <MaintainerClaim/> : isDiscovery ? <Discovery sessionId={sessionId}/> : allocationId ? <AllocationReview planId={allocationId}/> : fundId ? <FundScreen planId={fundId}/> : payoutsId ? <PayoutsScreen planId={payoutsId}/> : <>
          <div className="page-heading">
            <div><div className="eyebrow">FUNDING WORKSPACE</div><h1>Open-source funding operations</h1><p className="intro">Map installed Debian packages to upstream projects, review a deterministic plan, then approve each payment step.</p></div>
            <span className="date-stamp">UP TO 5,000 SOURCES</span>
          </div>

          <section className="workflow-strip" aria-label="Funding workflow">
            <div className="workflow-step current"><span className="step-index">01</span><span><strong>Discover</strong><small>Package inventory</small></span></div>
            <span className="workflow-line" aria-hidden="true"/>
            <div className="workflow-step"><span className="step-index">02</span><span><strong>Allocate</strong><small>Project shares</small></span></div>
            <span className="workflow-line" aria-hidden="true"/>
            <div className="workflow-step"><span className="step-index">03</span><span><strong>Fund</strong><small>Explicit capture</small></span></div>
            <span className="workflow-line" aria-hidden="true"/>
            <div className="workflow-step"><span className="step-index">04</span><span><strong>Distribute</strong><small>Separate approval</small></span></div>
          </section>

          <section className="start-panel" aria-labelledby="start-title">
            <div className="panel-topline"><span>GET STARTED</span><span className="panel-code">DV · 001</span></div>
            <div className="panel-content">
              <div className="panel-copy"><div className="scan-symbol"><span/><span/><span/></div><h2 id="start-title">Create a funding session</h2><p>Import a JSON report from the host package scanner, or use the bundled Debian fixture to review the workflow.</p>
                <div className="panel-actions"><input ref={uploadInput} hidden type="file" accept=".json,application/json" aria-label="Choose Debian scan JSON" onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ''; if (file) upload.mutate(file) }}/><button className="button button-primary" type="button" disabled={upload.isPending} onClick={() => uploadInput.current?.click()}>{upload.isPending ? 'Importing scan report…' : 'Import scan report'}<span aria-hidden="true">→</span></button><a className="button button-secondary" href="/discovery">Use demo packages</a><button className="button button-secondary" type="button" disabled={syntheticDemo.isPending} onClick={() => syntheticDemo.mutate()}>{syntheticDemo.isPending ? 'Creating synthetic demo…' : 'Create Synthetic Sandbox Demo'}</button></div>
                {syntheticDemo.isError && <div className="discovery-error" role="alert"><span>{syntheticDemo.error.message}</span>{syntheticDemo.error instanceof ApiError && syntheticDemo.error.requestId && <code>Request ID {syntheticDemo.error.requestId}</code>}<button className="text-button" type="button" onClick={() => syntheticDemo.mutate()}>Retry</button></div>}
                <p className="synthetic-demo-copy">Synthetic payment workflow only. Contributors and contribution evidence are fictional.</p>
                {upload.isError && <div role="alert" className="discovery-error"><span>{upload.error.message}</span>{upload.error instanceof ApiError && upload.error.requestId && <code>Request ID {upload.error.requestId}</code>}<button className="text-button" type="button" onClick={() => uploadInput.current?.click()}>Choose another file</button></div>}
              </div>
              <div className="panel-aside"><div className="aside-label">PAYMENT SAFEGUARDS</div><ol className="mini-steps"><li><span>01</span><div><strong>Review the frozen plan</strong><small>Amounts come from deterministic allocation rules.</small></div></li><li><span>02</span><div><strong>Approve the PayPal order</strong><small>Creating an order does not capture funds.</small></div></li><li><span>03</span><div><strong>Confirm capture and payout</strong><small>Funding and distribution are separate actions.</small></div></li></ol><p className="assurance"><span className="assurance-mark">✓</span> No payment runs without an explicit action.</p></div>
            </div>
          </section>

          <div className="below-grid below-grid-single">
            <WorkspaceActivity/>
          </div>
          <footer className="page-footer"><span>Dividend <span className="brand-period">·</span> Funding the software we depend on.</span><span>Payments remain in sandbox</span></footer>
          </>}
        </main>
      </div>
    </div>
  )
}
