import { useMutation, useQuery } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { ApiError, createDemoSession, createPlan, getProjects, getSession, type Project, type SourceDetail } from './api'

function ErrorMessage({ error, retry, message }: { error: Error; retry?: () => void; message?: string }) {
  const requestId = error instanceof ApiError ? error.requestId : undefined
  return <div className="discovery-error" role="alert"><span>{message || error.message}</span>{requestId && <code>Request ID {requestId}</code>}{retry && <button className="text-button" type="button" onClick={retry}>Retry</button>}</div>
}

function stateLabel(value: string) {
  return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}

function localToday() {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
}

function budgetToCents(value: string) {
  if (!/^\d{1,5}(?:\.\d{1,2})?$/.test(value)) throw new Error('Enter an amount from $1.00 to $10,000.00.')
  const [dollars, fraction = ''] = value.split('.')
  const cents = Number(dollars) * 100 + Number((fraction + '00').slice(0, 2))
  if (cents < 100 || cents > 1_000_000) throw new Error('Enter an amount from $1.00 to $10,000.00.')
  return cents
}

export default function Discovery({ sessionId }: { sessionId?: string }) {
  const [budget, setBudget] = useState('')
  const [evaluationDate, setEvaluationDate] = useState(localToday)
  const [formError, setFormError] = useState('')
  const start = useMutation({
    mutationFn: createDemoSession,
    onSuccess: ({ id }) => window.location.assign(`/discovery/${id}`),
  })
  const session = useQuery({
    queryKey: ['session', sessionId],
    queryFn: () => getSession(sessionId!),
    enabled: !!sessionId,
    refetchInterval: (query) => ['created', 'resolving'].includes(query.state.data?.status ?? '') ? 1000 : false,
  })
  const projects = useQuery({
    queryKey: ['projects', sessionId],
    queryFn: () => getProjects(sessionId!),
    enabled: !!sessionId && !!session.data,
    refetchInterval: () => ['created', 'resolving'].includes(session.data?.status ?? '') ? 3000 : false,
  })
  const plan = useMutation({
    mutationFn: ({ cents, date }: { cents: number; date: string }) => createPlan(sessionId!, cents, date),
    onSuccess: ({ id }, values) => window.location.assign(`/allocation/${id}?evaluation_date=${encodeURIComponent(values.date)}`),
  })

  function submitPlan(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setFormError('')
    try {
      if (!evaluationDate) throw new Error('Choose an evaluation date.')
      plan.mutate({ cents: budgetToCents(budget), date: evaluationDate })
    } catch (error) {
      setFormError(error instanceof Error ? error.message : 'Check the budget and date, then try again.')
    }
  }

  if (!sessionId) {
    return <section className="discovery-page">
      <a className="back-link" href="/">← Overview</a>
      <div className="discovery-heading"><div className="eyebrow">PACKAGE DISCOVERY</div><h1>Find the projects behind your packages.</h1><p>Start with the bundled Debian fixture. Dividend maps each binary package through its source package to an upstream project.</p></div>
      <section className="discovery-start" aria-labelledby="demo-title">
        <div className="discovery-start-copy"><span className="section-kicker">DETERMINISTIC FIXTURE</span><h2 id="demo-title">Explore demo packages</h2><p>Creates a discovery session from the included package scan, then resolves it with the bundled enrichment fixtures.</p>
          <button className="button button-primary" type="button" disabled={start.isPending} onClick={() => start.mutate()}><span>{start.isPending ? 'Creating session…' : 'Explore demo packages'}</span><span aria-hidden="true">→</span></button>
          {start.isError && <ErrorMessage error={start.error} retry={() => start.mutate()}/>}
        </div>
        <div className="discovery-start-note"><strong>What you’ll see</strong><p>Package membership, upstream mapping, and the method and outcome recorded for each source package.</p><a href="/">Return to overview</a></div>
      </section>
    </section>
  }

  if (session.isPending) return <section className="discovery-page"><a className="back-link" href="/">← Overview</a><div className="discovery-heading"><div className="eyebrow">PACKAGE DISCOVERY</div><h1>Loading session</h1></div><div className="skeleton session-skeleton"/><div className="skeleton table-skeleton"/></section>
  if (session.isError) return <section className="discovery-page"><a className="back-link" href="/">← Overview</a><div className="discovery-heading"><div className="eyebrow">PACKAGE DISCOVERY</div><h1>Session unavailable</h1></div><ErrorMessage error={session.error} retry={() => { void session.refetch() }}/></section>

  const record = session.data
  const projectList = projects.data?.projects ?? []
  const sourceRows = projects.data?.sources ?? []
  const projectsByKey = new Map(projectList.map((item) => [item.upstream_key, item]))
  const isResolving = ['created', 'resolving'].includes(record.status)

  return <section className="discovery-page">
    <a className="back-link" href="/">← Overview</a>
    <div className="discovery-heading discovery-heading-session"><div><div className="eyebrow">PACKAGE DISCOVERY</div><h1>Package discovery</h1><p>Binary packages are grouped by Debian source, then linked to the upstream projects returned by this session.</p></div><span className={`status-pill status-${record.status}`}><span/>{stateLabel(record.status)}</span></div>

    <section className="session-panel" aria-labelledby="session-summary-title">
      <div className="session-panel-head"><div><span className="section-kicker">SESSION</span><h2 id="session-summary-title">Resolution progress</h2></div><code className="session-id">{record.id}</code></div>
      {isResolving ? <div className="progress-block" aria-live="polite"><div className="progress-copy"><span>{stateLabel(record.stage || record.status)}</span><span>{record.progress_done.toLocaleString()} of {record.progress_total.toLocaleString()} source packages</span></div><div className="progress-track" role="progressbar" aria-label="Source package resolution" aria-valuemin={0} aria-valuemax={Math.max(record.progress_total, 1)} aria-valuenow={record.progress_done}><span style={{ width: `${record.progress_total ? (record.progress_done / record.progress_total) * 100 : 0}%` }}/></div></div> : <p className={`session-result ${record.status === 'failed' ? 'result-failed' : ''}`} role="status">{record.status === 'ready' ? 'Resolution complete. The table below reflects the latest project mappings.' : record.error_message || 'Resolution stopped. Review the session error and start a new discovery if needed.'}</p>}
      <div className="session-stats" aria-label="Session summary">
        <Summary label="Source packages" value={record.source_count.toLocaleString()}/>
        <Summary label="Binary packages" value={record.binary_count === null ? 'Not supplied' : record.binary_count.toLocaleString()}/>
        <Summary label="Upstream projects" value={projects.isPending ? '…' : projectList.length.toLocaleString()}/>
        <Summary label="Unmapped sources" value={projects.isPending ? '…' : (projects.data?.unmapped.length ?? 0).toLocaleString()}/>
        <Summary label="No upstream" value={projects.isPending ? '…' : (projects.data?.no_upstream.length ?? 0).toLocaleString()}/>
      </div>
    </section>

    {projects.isError && <ErrorMessage error={projects.error} retry={() => { void projects.refetch() }}/>}
    <section className="source-section" aria-labelledby="source-table-title">
      <div className="source-section-heading"><div><span className="section-kicker">PACKAGE MAP</span><h2 id="source-table-title">Source packages &amp; upstream projects</h2></div><span className="row-count">{projects.isPending ? 'Loading' : `${sourceRows.length.toLocaleString()} sources`}</span></div>
      {projects.isPending ? <div className="skeleton table-skeleton" aria-label="Loading source package mappings"/> : sourceRows.length === 0 ? <div className="empty-state"><h3>No source package details returned</h3><p>The session has no source rows to display.</p></div> : <div className="table-scroll" role="region" aria-label="Source package mappings" tabIndex={0}><table className="source-table"><thead><tr><th scope="col">Source package</th><th scope="col">Binary packages</th><th scope="col">Upstream project</th><th scope="col">Resolution</th></tr></thead><tbody>{sourceRows.map((source) => <SourceRow key={source.source_name} source={source} project={source.upstream_key ? projectsByKey.get(source.upstream_key) : undefined}/>)}</tbody></table></div>}
    </section>

    {record.status === 'ready' && !projects.isPending && !projects.isError && <section className="continue-panel" aria-labelledby="continue-title"><div className="continue-copy"><span className="section-kicker">NEXT STEP</span><h2 id="continue-title">Prepare an allocation plan</h2><p>Choose a budget and evaluation date. The server builds the plan from the resolved projects.</p></div>{projectList.length === 0 ? <p className="no-eligible">No mapped upstream projects are available for an allocation plan.</p> : <form className="plan-form" onSubmit={submitPlan}>
      <label>Budget (USD)<input required name="budget-usd" autoComplete="off" inputMode="decimal" type="number" min="1" max="10000" step="0.01" value={budget} onChange={(event) => setBudget(event.target.value)} placeholder="e.g. 25.00"/></label>
      <label>Evaluation date<input required name="evaluation-date" autoComplete="off" type="date" value={evaluationDate} onChange={(event) => setEvaluationDate(event.target.value)}/></label>
      <button className="button button-primary" type="submit" disabled={plan.isPending}>{plan.isPending ? 'Preparing plan…' : 'Continue to allocation'}<span aria-hidden="true">→</span></button>
      {formError && <p className="form-error" role="alert">{formError}</p>}{plan.isError && <ErrorMessage error={plan.error} message={plan.error instanceof ApiError && plan.error.status === 500 ? 'The server could not build a plan with this budget. Try a larger budget.' : undefined} retry={() => { try { plan.mutate({ cents: budgetToCents(budget), date: evaluationDate }) } catch (error) { setFormError(error instanceof Error ? error.message : 'Check the budget and date, then try again.') } }}/>} 
    </form>}</section>}
  </section>
}

function Summary({ label, value }: { label: string; value: string }) {
  return <div className="summary-item"><span>{label}</span><strong>{value}</strong></div>
}

function SourceRow({ source, project }: { source: SourceDetail; project?: Project }) {
  const repo = project?.repository_url
  const safeRepo = repo && /^https?:\/\//i.test(repo) ? repo : undefined
  return <tr>
    <th scope="row"><code>{source.source_name}</code></th>
    <td data-label="Binary packages">{source.binary_names === null ? <span className="cell-muted">Binary membership not provided</span> : source.binary_names.length ? <div className="binary-list">{source.binary_names.map((binary) => <code key={binary}>{binary}</code>)}</div> : <span className="cell-muted">No binary packages</span>}</td>
    <td data-label="Upstream project">{project ? <div className="upstream-cell"><strong>{project.display_name}</strong>{safeRepo && <a href={safeRepo} target="_blank" rel="noreferrer">{source.upstream_key}<span className="sr-only"> (opens in a new tab)</span></a>}</div> : source.outcome === 'no_upstream' ? <span className="cell-muted">No upstream project</span> : <span className="cell-muted">Not mapped</span>}</td>
    <td data-label="Resolution"><div className="resolution-cell"><span className={`outcome-badge outcome-${source.outcome}`}>{stateLabel(source.outcome)}</span><span className="method-label">{stateLabel(source.method)}</span></div></td>
  </tr>
}
