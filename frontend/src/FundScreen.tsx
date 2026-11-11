import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { approvePlan, capturePlan, getPlan, ApiError } from './api'
import { paypalReturnState } from './planAccess.js'
import { captureConfirmationMessage } from './paymentConfirmation.js'

function money(cents: number | null) {
  return cents === null ? 'Unknown' : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(cents / 100)
}

export default function FundScreen({ planId }: { planId: string }) {
  const client = useQueryClient()
  const plan = useQuery({ queryKey: ['plan', planId], queryFn: () => getPlan(planId) })
  const approve = useMutation({
    mutationFn: () => approvePlan(planId),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ['plan', planId] }) },
  })
  const capture = useMutation({
    mutationFn: () => capturePlan(planId),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ['plan', planId] }) },
  })

  if (plan.isPending) return <section className="discovery-page"><a className="back-link" href="/">← Overview</a><div className="discovery-heading"><div className="eyebrow">FUNDING</div><h1>Loading funding status…</h1></div><div className="skeleton session-skeleton"/></section>
  if (plan.isError) {
    const missingAccess = plan.error instanceof ApiError && plan.error.code === 'plan_auth_required'
    return <section className="discovery-page"><a className="back-link" href="/">← Overview</a><div className="discovery-heading"><div className="eyebrow">FUNDING</div><h1>Plan unavailable</h1></div><PaymentError error={plan.error} retry={missingAccess ? undefined : () => { void plan.refetch() }}/></section>
  }

  const record = plan.data
  const paypalState = paypalReturnState(window.location.search)
  const returnedFromPayPal = paypalState === 'return'
  const cancelledAtPayPal = paypalState === 'cancel'
  const uncertain = record.state === 'funding' && !record.order_id
  const approvalUnavailable = record.charge_cents === null || record.est_payout_fees_cents === null
  const approval = approve.data

  return <section className="discovery-page funding-page">
    <a className="back-link" href={`/allocation/${record.id}`}>← Allocation review</a>
    <div className="discovery-heading discovery-heading-session"><div><div className="eyebrow">FUNDING</div><h1>Funding status.</h1><p>Plan state and payment details reported by the backend.</p></div><span className={`status-pill status-${uncertain ? 'uncertain' : record.state}`}><span/>{paymentState(record.state, record.order_id, uncertain)}</span></div>

    <section className="session-panel allocation-summary" aria-labelledby="funding-summary-title">
      <div className="session-panel-head"><div><span className="section-kicker">PAYMENT SUMMARY</span><h2 id="funding-summary-title">Plan {record.id.slice(0, 8)}</h2></div>{record.order_id && <code className="session-id">Order {record.order_id}</code>}</div>
      <div className="session-stats allocation-stats">
        <Summary label="Plan budget" value={money(record.budget_cents)}/>
        <Summary label="Payable allocations" value={money(record.payable_total_cents)}/>
        <Summary label="Estimated incoming fee" value={record.est_incoming_fee_cents === null ? 'Unknown' : money(record.est_incoming_fee_cents)}/>
        <Summary label="Estimated payout fees" value={record.est_payout_fees_cents === null ? 'Unknown' : money(record.est_payout_fees_cents)}/>
        <Summary label="Funding charge" value={money(record.charge_cents)}/>
        <Summary label="Provider-confirmed capture" value={record.capture_gross_cents === null ? 'None recorded' : money(record.capture_gross_cents)}/>
      </div>
      <p className="allocation-summary-note"><strong>{summary(record.state, uncertain, record.order_id, record.capture_gross_cents)}</strong>{record.est_payout_fees_cents === null && ' This plan has no payable US recipient or supported fee scope, so approval and capture are blocked.'}{record.fee_policy_version && ` Fee estimates use ${record.fee_policy_version}; actual PayPal fees are recorded from provider responses.`}{record.failure_reason && ` Backend failure reason: ${record.failure_reason}.`}</p>
    </section>

    <section className="continue-panel approval-panel funding-action-panel" aria-labelledby="funding-action-title">
      <div className="continue-copy"><span className="section-kicker">PAYMENT ACTION</span><h2 id="funding-action-title">{record.state === 'draft' ? 'Review approval eligibility' : 'Current payment status'}</h2><p>{actionDescription(record.state, uncertain, returnedFromPayPal, cancelledAtPayPal, record.order_id)}</p></div>
      <div className="approval-actions">
        {record.state === 'draft' && <button className="button button-primary" type="button" disabled={approve.isPending || approvalUnavailable} title={approvalUnavailable ? 'Approval requires measured payment fee inputs.' : undefined} onClick={() => approve.mutate()}>{approve.isPending ? 'Checking fee inputs…' : approvalUnavailable ? 'Approval unavailable · fees unmeasured' : 'Create PayPal order'}</button>}
        {record.state === 'funding' && record.order_id && !returnedFromPayPal && <button className="button button-secondary" type="button" disabled={approve.isPending} onClick={() => approve.mutate()}>{approve.isPending ? 'Retrieving approval link…' : cancelledAtPayPal ? 'Reopen PayPal approval' : 'Retrieve existing PayPal approval link'}</button>}
        {approval && <a className="button button-secondary" href={approval.approval_url}>Continue to PayPal Sandbox ↗</a>}
        {record.state === 'funding' && record.order_id && returnedFromPayPal && record.charge_cents !== null && <button className="button button-primary" type="button" disabled={capture.isPending} onClick={() => {
          if (window.confirm(captureConfirmationMessage(record.charge_cents))) capture.mutate()
        }}>{capture.isPending ? 'Confirming capture…' : 'Confirm and capture funds'}</button>}
        {(uncertain || (record.state === 'funding' && record.order_id && !returnedFromPayPal) || capture.isError) && <button className="button button-secondary" type="button" disabled={plan.isFetching} onClick={() => { approve.reset(); capture.reset(); void plan.refetch() }}>{plan.isFetching ? 'Checking status…' : 'Check plan status'}</button>}
        {approve.isError && <PaymentError error={approve.error} retry={approve.error instanceof ApiError && !['order_outcome_unknown', 'charge_unavailable'].includes(approve.error.code ?? '') ? () => approve.mutate() : undefined} refresh={() => { approve.reset(); void plan.refetch() }}/ >}
        {capture.isError && <PaymentError error={capture.error} refresh={() => { capture.reset(); void plan.refetch() }}/ >}
        {approve.isSuccess && <p className="form-success" role="status">PayPal approval is ready for order {approve.data.order_id}. No funds have been captured.</p>}
        {capture.isSuccess && <div className="form-success" role="status"><strong>PayPal Sandbox capture response</strong><p>Capture reference <code>{capture.data.capture.capture_id}</code> · {capture.data.capture.status} · {capture.data.capture.currency}</p><p>Gross {money(capture.data.capture.gross_cents)} · PayPal fee {money(capture.data.capture.fee_cents)} · Net {money(capture.data.capture.net_cents)} · plan state {label(capture.data.state)}</p></div>}
      </div>
    </section>
    <p className="funding-footnote">An order is not a completed payment. Funds are shown as received only after PayPal confirms capture. No payout is initiated here.</p>
  </section>
}

function PaymentError({ error, retry, refresh }: { error: Error; retry?: () => void; refresh?: () => void }) {
  const unknown = error instanceof ApiError && error.code === 'order_outcome_unknown'
  return <div className="discovery-error" role="alert"><span>{unknown ? 'The order result is uncertain. Do not submit another approval request until the plan status has been checked.' : error.message}</span>
    {error instanceof ApiError && error.code && <code>{error.code}</code>}{error instanceof ApiError && error.requestId && <code>Request ID {error.requestId}</code>}
    {retry && <button className="text-button" type="button" onClick={retry}>Try again</button>}{refresh && <button className="text-button" type="button" onClick={refresh}>Check status</button>}
  </div>
}

function Summary({ label, value }: { label: string; value: string }) { return <div className="summary-item"><span>{label}</span><strong>{value}</strong></div> }
function label(state: string) { return state.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()) }
function paymentState(state: string, orderId: string | null, uncertain: boolean) {
  if (uncertain) return 'Order outcome uncertain'
  if (state === 'draft') return 'Draft · not approved'
  if (state === 'funding' && orderId) return 'Approved · order created'
  if (state === 'funded') return 'Funded · capture confirmed'
  if (state === 'failed') return 'Failed'
  return label(state)
}
function summary(state: string, uncertain: boolean, orderId: string | null, gross: number | null) {
  if (uncertain) return 'The backend is in funding state without a confirmed order ID. The order outcome is uncertain.'
  if (gross !== null) return `PayPal confirmed a capture of ${money(gross)}.`
  if (state === 'funding' && orderId) return 'An order exists. No capture is recorded yet.'
  if (state === 'draft') return 'This plan is not approved and no order exists.'
  if (state === 'failed') return 'The backend marked this funding attempt failed.'
  return `Backend plan state: ${label(state)}.`
}
function actionDescription(state: string, uncertain: boolean, returned: boolean, cancelled: boolean, orderId: string | null) {
  if (uncertain) return 'The order request may have reached the provider. Only check status; do not create another order.'
  if (state === 'draft') return 'Approval creates an order only when measured fee inputs are available. It does not capture funds.'
  if (state === 'funding' && orderId && cancelled) return 'PayPal approval was canceled. No funds were captured; you can reopen the existing approval.'
  if (state === 'funding' && orderId && returned) return 'PayPal returned to this plan. Review the amount, then explicitly confirm capture.'
  if (state === 'funding' && orderId && !returned) return 'An order is recorded. Retrieve its PayPal approval link or return from PayPal to continue.'
  if (state === 'funding' && orderId) return 'PayPal returned to this plan. Review the amount, then explicitly confirm capture.'
  if (state === 'funded') return 'Capture is confirmed by the backend. No payment action is available on this screen.'
  return 'No payment action is available for the current backend state.'
}
