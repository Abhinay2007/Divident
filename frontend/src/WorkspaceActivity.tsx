import { useQueries, useQuery } from '@tanstack/react-query'
import { ApiError, getPlan, getPayouts, getWorkspace, type Plan, type Payouts } from './api'
import { continuePath, hasWorkspaceActivity } from './workspace.js'

function label(value: string) { return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()) }
function date(value: string) { return new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) }

function payoutStatus(plan: Plan, payouts?: Payouts) {
  const latest = [...(payouts?.batches ?? [])].sort((a, b) => a.attempt - b.attempt).at(-1)
  if (!latest) return plan.state === 'funded' ? 'Ready to distribute' : plan.state === 'paying' ? 'Pending' : 'No payout batch'
  const statuses = latest.items.map((item) => item.status)
  if (latest.status === 'needs_attention' || statuses.includes('needs_attention')) return 'Needs attention'
  if (plan.state === 'paying' || ['intent', 'pending'].includes(latest.status) || statuses.includes('pending')) return 'Pending'
  if (!statuses.length) return latest.status === 'failed' ? 'Failed' : 'Item status unavailable'
  if (statuses.length && statuses.every((status) => status === 'success')) return 'Successful'
  if (statuses.some((status) => ['failed', 'returned'].includes(status))) return statuses.some((status) => status === 'success') ? 'Partially successful' : 'Failed'
  if (statuses.includes('unclaimed')) return 'Unclaimed'
  return label(latest.status)
}

function payoutActivity(payouts?: Payouts) {
  const latest = [...(payouts?.batches ?? [])].sort((a, b) => b.attempt - a.attempt)[0]
  if (!latest) return 'No payout submitted'
  const counts = new Map<string, number>()
  for (const item of latest.items) counts.set(item.status, (counts.get(item.status) ?? 0) + 1)
  return `${latest.items.length} recipients · ${[...counts].map(([status, count]) => `${label(status)} ${count}`).join(' · ') || label(latest.status)}`
}

export default function WorkspaceActivity() {
  const workspace = useQuery({ queryKey: ['workspace'], queryFn: getWorkspace })
  const sessions = workspace.data?.sessions ?? []
  const refs = sessions.flatMap((session) => session.plans.map((plan) => ({ ...plan, sessionId: session.id })))
  const planQueries = useQueries({ queries: refs.map(({ id }) => ({ queryKey: ['plan', id], queryFn: () => getPlan(id), retry: false })) })
  const payoutQueries = useQueries({ queries: refs.map(({ id }, index) => ({
    queryKey: ['payouts', id], queryFn: () => getPayouts(id), enabled: !!planQueries[index]?.data,
    refetchInterval: planQueries[index]?.data?.state === 'paying' ? 3000 : false,
  })) })

  if (workspace.isPending) return <section className="quiet-card" aria-labelledby="activity-title"><div className="card-heading"><h2 id="activity-title">Recent activity</h2><span className="muted-label">THIS WORKSPACE</span></div><div className="skeleton table-skeleton" aria-label="Loading saved workspace"/></section>
  if (workspace.isError) return <section className="quiet-card" aria-labelledby="activity-title"><div className="card-heading"><h2 id="activity-title">Recent activity</h2><span className="muted-label">THIS WORKSPACE</span></div><div className="discovery-error" role="alert"><span>{workspace.error.message}</span>{workspace.error instanceof ApiError && workspace.error.requestId && <code>Request ID {workspace.error.requestId}</code>}<button className="text-button" type="button" onClick={() => { void workspace.refetch() }}>Retry</button></div></section>

  return <section className="quiet-card" aria-labelledby="workspace-title">
    <div className="card-heading"><h2 id="workspace-title">Recent activity</h2><a className="muted-label" href="/discovery">ALL SESSIONS →</a></div>
    {!hasWorkspaceActivity(sessions) ? <div className="empty-activity"><span className="empty-indicator"/><p>No saved sessions yet. Start a discovery to begin; leaving a workflow does not delete it.</p></div> : <div className="workspace-sessions">
      {sessions.map((session) => <article className="workspace-session" key={session.id}>
        <div className="workspace-session-head"><div><span className="section-kicker">{session.synthetic_demo ? 'SYNTHETIC SANDBOX DEMO' : `${label(session.mode)} SESSION`}</span><h3>{session.source_count.toLocaleString()} source packages</h3><p>Last activity {date(session.last_updated_at)}</p></div><span className={`status-pill status-${session.status}`}><span/>{label(session.status)}</span></div>
        <a className="text-link" href={`/discovery/${session.id}`}>Continue discovery →</a>
        {session.plans.length > 0 && <div className="workspace-plans">{session.plans.map((planRef) => {
          const index = refs.findIndex((ref) => ref.id === planRef.id)
          const planQuery = planQueries[index]
          const payouts = payoutQueries[index]?.data
          const record = planQuery?.data
          const unauthorized = planQuery?.error instanceof ApiError && planQuery.error.code === 'plan_auth_required'
          return <div className="workspace-plan" key={planRef.id}>
            <div className="workspace-plan-main"><span className="section-kicker">{planRef.synthetic_demo ? 'SYNTHETIC SANDBOX DEMO PLAN' : 'FUNDING PLAN'}</span><strong>Plan {planRef.id.slice(0, 8)}</strong>
              {record ? <><span className={`status-pill status-${record.state}`}><span/>{label(record.state)}</span><small>{record.capture_gross_cents !== null ? `Capture confirmed · $${(record.capture_gross_cents / 100).toFixed(2)}` : record.state === 'funding' ? 'PayPal order created · capture pending' : 'No capture confirmed'}</small><small>Payouts: {payouts ? payoutStatus(record, payouts) : record.state === 'funded' ? 'Ready to distribute' : 'Loading status…'}{payouts && ` · ${payoutActivity(payouts)}`}</small></> : <span className="cell-muted">{unauthorized ? 'Plan access is unavailable in this browser.' : planQuery?.isError ? 'Plan status unavailable.' : 'Loading plan status…'}</span>}
              <small>Last updated {date(planRef.last_updated_at)}</small>
              {payoutQueries[index]?.isError && <small className="inline-error">Payout activity unavailable.</small>}
            </div>
            {record ? <a className="button button-secondary" href={continuePath(record.state, record.id)}>Continue plan <span aria-hidden="true">→</span></a> : <span className="cell-muted" aria-disabled="true">Plan access unavailable</span>}
          </div>
        })}</div>}
      </article>)}
    </div>}
  </section>
}
