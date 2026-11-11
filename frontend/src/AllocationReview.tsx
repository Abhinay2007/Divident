import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { adjustPlanBudget, approvePlan, getPlan, getPlanEvidenceReview, getPlanExplanations, getProjects, ApiError, type Plan, type Project } from './api'
import { evidenceReviewPresentation } from './evidenceReview.js'
import { approvalUnavailableReason } from './paymentConfirmation.js'
import SyntheticDemoBanner from './SyntheticDemoBanner'

function money(cents: number) {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(cents / 100)
}

function localToday() {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
}

function displayDate(value: string) {
  return new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeZone: 'UTC' }).format(new Date(`${value}T00:00:00Z`))
}

function budgetToCents(value: string) {
  if (!/^\d{1,5}(?:\.\d{1,2})?$/.test(value)) throw new Error('Enter a budget from $1.00 to $10,000.00.')
  const [dollars, cents = ''] = value.split('.')
  const amount = Number(dollars) * 100 + Number((cents + '00').slice(0, 2))
  if (amount < 100 || amount > 1_000_000) throw new Error('Enter a budget from $1.00 to $10,000.00.')
  return amount
}

function stateLabel(value: string) {
  return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}

function signalValue(value: string | number | boolean | null) {
  if (value === null) return 'Not supplied'
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  return String(value)
}

function ErrorMessage({ error, retry }: { error: Error; retry?: () => void }) {
  return <div className="discovery-error" role="alert"><span>{error.message}</span>
    {error instanceof ApiError && error.requestId && <code>Request ID {error.requestId}</code>}
    {retry && <button className="text-button" type="button" onClick={retry}>Retry</button>}
  </div>
}

export default function AllocationReview({ planId }: { planId: string }) {
  const client = useQueryClient()
  const [evaluationDate, setEvaluationDate] = useState(() => {
    const value = new URLSearchParams(window.location.search).get('evaluation_date')
    return value && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : localToday()
  })
  const [budgetDraft, setBudgetDraft] = useState<string | null>(null)
  const [formError, setFormError] = useState('')
  const plan = useQuery({ queryKey: ['plan', planId], queryFn: () => getPlan(planId) })
  const projects = useQuery({
    queryKey: ['projects', plan.data?.session_id],
    queryFn: () => getProjects(plan.data!.session_id),
    enabled: !!plan.data && !plan.data.synthetic_demo,
  })
  const explanations = useQuery({
    queryKey: ['explanations', planId],
    queryFn: () => getPlanExplanations(planId),
    enabled: !!plan.data,
  })
  const evidenceReview = useMutation({ mutationFn: () => getPlanEvidenceReview(planId) })
  const adjust = useMutation({
    mutationFn: ({ cents, date }: { cents: number; date: string }) => adjustPlanBudget(planId, cents, date),
    onSuccess: (updated) => {
      client.setQueryData(['plan', planId], updated)
      setBudgetDraft((updated.budget_cents / 100).toFixed(2))
      setFormError('')
      approve.reset()
    },
  })
  const approve = useMutation({
    mutationFn: () => approvePlan(planId),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ['plan', planId] }) },
  })

  if (plan.isPending) return <section className="discovery-page"><a className="back-link" href="/discovery">← Package discovery</a><div className="discovery-heading"><div className="eyebrow">ALLOCATION REVIEW</div><h1>Loading allocation plan…</h1></div><div className="skeleton session-skeleton"/><div className="skeleton table-skeleton"/></section>
  if (plan.isError) return <section className="discovery-page"><a className="back-link" href="/discovery">← Package discovery</a><div className="discovery-heading"><div className="eyebrow">ALLOCATION REVIEW</div><h1>Plan unavailable</h1></div><ErrorMessage error={plan.error} retry={() => { void plan.refetch() }}/></section>

  const record = plan.data
  const projectByKey = new Map((projects.data?.projects ?? []).map((project) => [project.upstream_key, project]))
  const isDraft = record.state === 'draft'
  const approvalUnavailable = approvalUnavailableReason(record)
  const approvalResult = approve.data
  const currentBudget = budgetDraft ?? (record.budget_cents / 100).toFixed(2)
  const aiReview = evidenceReviewPresentation(evidenceReview.data)

  function submitBudget(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setFormError('')
    try {
      adjust.mutate({ cents: budgetToCents(currentBudget), date: evaluationDate })
    } catch (error) {
      setFormError(error instanceof Error ? error.message : 'Check the budget and evaluation date.')
    }
  }

  return <section className="discovery-page allocation-review">
    <a className="back-link" href={`/discovery/${record.session_id}`}>← Package discovery</a>
    {record.synthetic_demo && <SyntheticDemoBanner/>}
    <div className="discovery-heading discovery-heading-session">
      <div><div className="eyebrow">ALLOCATION REVIEW</div><h1>Review the funding plan.</h1><p>Inspect each project’s share, eligibility, and the signals behind the allocation before requesting PayPal approval.</p></div>
      <span className={`status-pill ${isDraft ? 'status-draft' : `status-${record.state}`}`}><span/>{isDraft ? 'Draft · not approved' : stateLabel(record.state)}</span>
    </div>

    <section className="session-panel allocation-summary" aria-labelledby="funding-summary-title">
      <div className="session-panel-head"><div><span className="section-kicker">FUNDING OVERVIEW</span><h2 id="funding-summary-title">Plan summary</h2></div><code className="session-id">{record.id}</code></div>
      <div className="money-summary" aria-label="Plan money summary">
        <Summary label="Charged now" value={record.money_summary.charged_now_cents === null ? 'Not charged' : money(record.money_summary.charged_now_cents)}/>
        <Summary label="Pledged, not charged" value={money(record.money_summary.pledged_cents)}/>
        <Summary label="PayPal fee estimate" value={record.money_summary.fee_estimate_cents === null ? 'Unavailable' : money(record.money_summary.fee_estimate_cents)}/>
      </div>
      <p className="allocation-summary-note"><span>Budget <strong>{money(record.budget_cents)}</strong></span><span>Payable <strong>{money(record.payable_total_cents)}</strong></span><span>Reserved <strong>{money(record.reserved_total_cents)}</strong></span><span>{record.lines.length.toLocaleString()} projects</span></p>
      <p className="allocation-summary-note allocation-summary-detail"><strong>{isDraft ? 'Draft · no order has been created.' : `Plan state: ${stateLabel(record.state)}.`}</strong>
        {record.est_payout_fees_cents === null ? ' A supported recipient and measured fee scope are required before funding is available.' : ` Estimates use ${record.fee_policy_version}; actual fees come from PayPal responses.`}
      </p>
    </section>

    <section className="source-section allocation-lines" aria-labelledby="allocation-lines-title">
      <div className="source-section-heading"><div><span className="section-kicker">PLAN LINES</span><h2 id="allocation-lines-title">Project allocations</h2></div><span className="row-count">{record.lines.length.toLocaleString()} projects</span></div>
      {projects.isError && <ErrorMessage error={projects.error} retry={() => { void projects.refetch() }}/>}
      {projects.isPending && <div className="skeleton table-skeleton" aria-label="Loading project and maintainer details"/>}
      {!projects.isPending && record.lines.length === 0 && <div className="empty-state"><h3>No project allocations</h3><p>This plan contains no allocation lines.</p></div>}
      {!projects.isPending && record.lines.length > 0 && <div className="table-scroll allocation-table-scroll" role="region" aria-label="Project allocation review" tabIndex={0}>
        <table className="source-table allocation-review-table">
          <thead><tr><th scope="col">Project &amp; maintainer</th><th scope="col">Allocation</th><th scope="col">Payable / reserved</th><th scope="col">Allocation basis</th></tr></thead>
          <tbody>{record.lines.map((line) => <AllocationRow key={line.upstream_key} line={line} project={projectByKey.get(line.upstream_key)}/>)}</tbody>
        </table>
      </div>}
    </section>

    {!record.synthetic_demo && <section className="source-section" aria-labelledby="ai-evidence-review-title">
      <div className="source-section-heading"><div><span className="section-kicker">OPTIONAL MODAL REVIEW</span><h2 id="ai-evidence-review-title">AI Evidence Review</h2><p>Modal summarizes frozen contribution evidence. It cannot change deterministic allocations or payment amounts.</p></div>
        <button className="button button-secondary" type="button" disabled={evidenceReview.isPending} onClick={() => evidenceReview.mutate()}>{evidenceReview.isPending ? 'Reviewing evidence…' : 'Run AI Evidence Review'}</button></div>
      {evidenceReview.isError && <ErrorMessage error={evidenceReview.error} retry={() => evidenceReview.mutate()}/>}
      {aiReview?.status === 'unavailable' && <div className="quiet-card" role="status"><strong>AI review unavailable</strong><p>{aiReview.reason?.replaceAll('_', ' ') ?? 'Modal could not review this proposal.'}</p><button className="text-button" type="button" disabled={evidenceReview.isPending} onClick={() => evidenceReview.mutate()}>Try again</button></div>}
      {aiReview?.status === 'invalid' && <div className="discovery-error" role="status"><strong>AI response not shown</strong><p>The response did not pass strict evidence validation{aiReview.reason ? ` (${aiReview.reason.replaceAll('_', ' ')}).` : '.'} No unsupported findings were displayed.</p><button className="text-button" type="button" disabled={evidenceReview.isPending} onClick={() => evidenceReview.mutate()}>Try again</button></div>}
      {aiReview?.status === 'available' && <article className="explanation-card ai-evidence-review" aria-label="Modal-generated evidence review">
        <div className="explanation-head"><h3>Executive summary</h3><span className="method-label">AI-generated · {aiReview.model}</span></div>
        <p>{aiReview.summary}</p>
        <h4>Evidence-based interpretation</h4>
        <p>Code reviews are documented participation, but do not mean a change was merged. Counts show distinct PRs in the supplied evidence, not review-event totals; they do not establish code quality, project impact, or current involvement.</p>
        <h4>Contributor highlights</h4>
        {aiReview.contributorGroups.map((group) => <section key={group.label} aria-label={group.label}>
          <h5>{group.label}</h5><ul className="explanation-claims">{group.contributors.map((finding) => <li key={finding.contributor_id}>
            <strong>Contributor {finding.contributor_id}</strong><p>{finding.observation}</p>
            <p className="muted-label">{finding.merged_pr_count > 0 && `${finding.merged_pr_count} merged ${finding.merged_pr_count === 1 ? 'PR' : 'PRs'}`}{finding.merged_pr_count > 0 && finding.reviewed_pr_count > 0 && ' · '}{finding.reviewed_pr_count > 0 && `${finding.reviewed_pr_count} reviewed ${finding.reviewed_pr_count === 1 ? 'PR' : 'PRs'}`}</p>
            {finding.citations.length > 0 && <ul aria-label={`Verified evidence for contributor ${finding.contributor_id}`}>
              {finding.citations.map((citation) => <li key={`${citation.activity_type}-${citation.pr_number}-${citation.url}`}>
                <span>{citation.activity_type === 'merged_pull_request' ? 'Merged PR' : 'Reviewed PR'} </span>
                <a href={citation.url} target="_blank" rel="noreferrer">#{citation.pr_number} ↗<span className="sr-only"> (opens in a new tab)</span></a>
              </li>)}
            </ul>}
          </li>)}</ul>
        </section>)}
        {aiReview.advisoryFlags.length > 0 && <p className="muted-label">Signals: {aiReview.advisoryFlags.map((flag) => flag.replaceAll('_', ' ')).join(' · ')}</p>}
      </article>}
    </section>}

    {!record.synthetic_demo && <section className="source-section" aria-labelledby="why-funded-title">
      <div className="source-section-heading"><div><span className="section-kicker">DETERMINISTIC ALLOCATION</span><h2 id="why-funded-title">Why this project is funded</h2><p>These server-authored explanations describe the allocation rules and remain separate from the optional AI review.</p></div><span className="row-count">One explanation per line</span></div>
      {explanations.isPending && <div className="skeleton table-skeleton" aria-label="Preparing evidence explanations"/>}
      {explanations.isError && <ErrorMessage error={explanations.error} retry={() => { void explanations.refetch() }}/>}
      {explanations.data?.explanations.map((item) => <article className="explanation-card" key={item.upstream_key}>
        <div className="explanation-head"><h3>{item.upstream_key}</h3><span className="method-label">{item.status === 'disabled' ? 'Deterministic explanation' : 'Deterministic fallback'}</span></div>
        <ul className="explanation-claims">{item.claims.map((claim) => <li key={claim.id}><p>{claim.text}</p><div className="claim-refs">Evidence: {claim.evidence_ids.map((id) => <code key={id}>{id}</code>)}</div></li>)}</ul>
        <details className="signal-details"><summary>Inspect supporting evidence</summary><div className="evidence-source-list">{item.evidence.map((evidence) => <div className="evidence-source" key={evidence.id}><strong>{evidence.id}</strong><code>{JSON.stringify(evidence)}</code></div>)}</div></details>
      </article>)}
    </section>}

    {isDraft && !record.synthetic_demo && <section className="continue-panel allocation-actions" aria-labelledby="adjust-budget-title">
      <div className="continue-copy"><span className="section-kicker">PLAN CONTROLS</span><h2 id="adjust-budget-title">Adjust the budget</h2><p>Changing the budget recalculates project amounts using the same deterministic allocation rules.</p></div>
      <form className="budget-adjust-form" onSubmit={submitBudget}>
        <label>Funding budget (USD)<input required name="budget-usd" autoComplete="off" inputMode="decimal" type="number" min="1" max="10000" step="0.01" value={currentBudget} disabled={adjust.isPending || approve.isPending} onChange={(event) => setBudgetDraft(event.target.value)}/></label>
        <label>Evaluation date<input required name="evaluation-date" autoComplete="off" type="date" value={evaluationDate} disabled={adjust.isPending || approve.isPending} onChange={(event) => setEvaluationDate(event.target.value)}/></label>
        <button className="button button-secondary" type="submit" disabled={adjust.isPending || approve.isPending}>{adjust.isPending ? 'Updating plan…' : 'Update allocations'}</button>
        {formError && <p className="form-error" role="alert">{formError}</p>}
        {adjust.isError && <ErrorMessage error={adjust.error} retry={() => {
          try { adjust.mutate({ cents: budgetToCents(currentBudget), date: evaluationDate }) }
          catch (error) { setFormError(error instanceof Error ? error.message : 'Check the budget and evaluation date.') }
        }}/>}
        {adjust.isSuccess && <p className="form-success" role="status">Plan updated. Allocation lines reflect the new budget.</p>}
      </form>
    </section>}

    <section className="continue-panel approval-panel" aria-labelledby="approval-title">
      <div className="continue-copy"><span className="section-kicker">REVIEW BEFORE APPROVAL</span><h2 id="approval-title">{isDraft ? 'Request PayPal approval' : 'Funding status'}</h2>
        <p>{isDraft ? 'This request creates a PayPal order only when the required fee inputs are available. It does not capture funds or start maintainer payouts.' : 'A funding order is not the same as captured funds. Capture and payouts are handled in later steps.'}</p>
      </div>
      <div className="approval-actions">
        {isDraft && <button className="button button-primary" type="button" disabled={approve.isPending || adjust.isPending || !!approvalUnavailable} onClick={() => approve.mutate()}>{approve.isPending ? 'Checking approval…' : adjust.isPending ? 'Updating plan…' : approvalUnavailable ? 'Approval unavailable' : 'Request PayPal approval'}{!approvalUnavailable && <span aria-hidden="true">→</span>}</button>}
        {isDraft && approvalUnavailable && <p className="funding-footnote" role="status">{approvalUnavailable}</p>}
        {approvalResult && <a className="button button-secondary" href={approvalResult.approval_url}>Open PayPal approval<span aria-hidden="true">↗</span></a>}
        {approve.isError && <ErrorMessage error={approve.error} retry={approve.error instanceof ApiError && !['order_outcome_unknown', 'charge_unavailable'].includes(approve.error.code ?? '') ? () => approve.mutate() : undefined}/>}
        {approve.isSuccess && <p className="form-success" role="status">PayPal order {approve.data.order_id} is ready for buyer authorization. No funds have been captured.</p>}
        <a className="text-link" href={`/fund/${record.id}`}>View funding status <span aria-hidden="true">→</span></a>
        {!isDraft && <span className="status-pill"><span/>{stateLabel(record.state)}</span>}
      </div>
    </section>
  </section>
}

function Summary({ label, value }: { label: string; value: string }) {
  return <div className="summary-item"><span>{label}</span><strong>{value}</strong></div>
}

function AllocationRow({ line, project }: { line: Plan['lines'][number]; project?: Project }) {
  const repo = project?.repository_url
  const safeRepo = repo && /^https?:\/\//i.test(repo) ? repo : undefined
  const signals = line.facts_snapshot?.normalized
  const sourceFacts = line.facts_snapshot?.sources ?? []
  const eligible = line.payability === 'payable'
  return <tr>
    <th scope="row" data-label="Project & maintainer">
      <div className="allocation-project"><strong>{line.display_name}</strong><code>{line.upstream_key}</code>
        {safeRepo ? <a href={safeRepo} target="_blank" rel="noreferrer">{safeRepo}<span className="sr-only"> (opens in a new tab)</span></a> : <span className="cell-muted">Repository not supplied</span>}
        {line.maintainers.length > 0 ? <ul className="maintainer-lines">{line.maintainers.map((maintainer) => <li key={maintainer.maintainer_id}><span>{maintainer.display_name} · {(maintainer.share_bps / 100).toFixed(2)}%</span><strong>{money(maintainer.amount_cents)}</strong></li>)}</ul> : <span className="maintainer-line">{project?.maintainer_name ?? 'No eligible maintainer'}</span>}
        <span className={`eligibility-pill ${eligible ? 'eligible' : 'reserved'}`}>{eligible ? 'Opted in · payable' : 'No opted-in US maintainer · reserved'}</span>
      </div>
    </th>
    <td data-label="Allocation"><strong className="allocation-amount">{money(line.amount_cents)}</strong></td>
    <td data-label="Payable / reserved"><div className="allocation-split"><span>Payable <strong>{money(line.payable_cents)}</strong></span><span>Reserved <strong>{money(line.reserved_cents)}</strong></span></div></td>
    <td data-label="Allocation basis">
      <p className="allocation-reason">{line.reason}</p>
      <details className="signal-details"><summary>View recorded signals &amp; evidence</summary>
        {signals ? <dl className="signal-grid">
          <Signal label="Binary packages" value={signals.binary_count}/><Signal label="Archived" value={signals.archived}/>
          <Signal label="Last commit" value={signals.last_commit ? displayDate(signals.last_commit) : null}/>
          <Signal label="Committers (12 months)" value={signals.committers_12m}/><Signal label="Funding file" value={signals.funding_yml}/>
          <Signal label="Partial enrichment" value={signals.partial}/>
        </dl> : <p className="cell-muted">No normalized fact snapshot was supplied for this line.</p>}
        {sourceFacts.map((source) => <div className="evidence-source" key={source.source_name}><strong>{source.source_name}</strong>
          {source.repository_url && <span>Repository fact: <code>{source.repository_url}</code></span>}
          {source.resolution.evidence?.length ? <ul>{source.resolution.evidence.map((item, index) => <li key={`${item.field}-${index}`}><code>{item.field}</code> = <code>{item.value}</code></li>)}</ul> : <span>No resolver evidence recorded.</span>}
        </div>)}
      </details>
    </td>
  </tr>
}

function Signal({ label, value }: { label: string; value: string | number | boolean | null }) {
  return <div><dt>{label}</dt><dd>{signalValue(value)}</dd></div>
}
