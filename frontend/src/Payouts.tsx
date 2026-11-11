import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { dispatchPayouts, getPlan, getPayouts, reconcilePayouts, retryFailedPayouts, ApiError, type PayoutBatch, type PayoutItem } from './api'
import { payoutConfirmationMessage, payoutPreviewValid } from './payoutConfirmation.js'
import SyntheticDemoBanner from './SyntheticDemoBanner'

function money(cents: number) {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(cents / 100)
}

function statusLabel(status: string) {
  return status.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}

function statusClass(status: string) {
  return status === 'success' ? 'payout-success' : status === 'failed' || status === 'returned' ? 'payout-failed' : status === 'needs_attention' ? 'payout-attention' : status === 'pending' || status === 'unclaimed' ? 'payout-pending' : ''
}

function currentItems(batches: PayoutBatch[]) {
  const current = new Map<string, PayoutItem>()
  for (const batch of [...batches].sort((a, b) => a.attempt - b.attempt)) {
    for (const item of batch.items) current.set(item.maintainer_id ?? item.upstream_key, item)
  }
  return [...current.values()]
}

function currentStatus(planState: string, batches: PayoutBatch[], items: PayoutItem[]) {
  if (batches.at(-1)?.status === 'needs_attention' || items.some((item) => item.status === 'needs_attention')) return 'Needs attention'
  if (!batches.length) return planState === 'funded' ? 'Ready to distribute' : planState === 'paying' ? 'Pending' : 'No payouts yet'
  if (planState === 'paying' || items.some((item) => item.status === 'pending')) return 'Pending'
  const succeeded = items.filter((item) => item.status === 'success').length
  const exceptions = items.some((item) => ['failed', 'returned', 'unclaimed'].includes(item.status))
  if (succeeded && exceptions) return 'Partially successful'
  if (succeeded && items.every((item) => item.status === 'success')) return 'Successful'
  if (exceptions) return 'Failed'
  return statusLabel(planState)
}

export default function Payouts({ planId }: { planId: string }) {
  const client = useQueryClient()
  const plan = useQuery({
    queryKey: ['plan', planId], queryFn: () => getPlan(planId),
    refetchInterval: (query) => query.state.data?.state === 'paying' ? 3000 : false,
  })
  const payouts = useQuery({
    queryKey: ['payouts', planId], queryFn: () => getPayouts(planId), enabled: !!plan.data,
    refetchInterval: plan.data?.state === 'paying' ? 3000 : false,
  })
  const refresh = async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: ['plan', planId] }),
      client.invalidateQueries({ queryKey: ['payouts', planId] }),
    ])
  }
  const dispatch = useMutation({ mutationFn: () => dispatchPayouts(planId), onSettled: refresh })
  const retry = useMutation({ mutationFn: () => retryFailedPayouts(planId), onSettled: refresh })
  const reconcile = useMutation({ mutationFn: () => reconcilePayouts(planId), onSettled: refresh })

  if (plan.isPending) return <section className="discovery-page"><a className="back-link" href="/">← Overview</a><div className="discovery-heading"><div className="eyebrow">PAYOUT TRACKING</div><h1>Loading payout status…</h1></div><div className="skeleton session-skeleton"/><div className="skeleton table-skeleton"/></section>
  if (plan.isError) return <section className="discovery-page"><a className="back-link" href="/">← Overview</a><div className="discovery-heading"><div className="eyebrow">PAYOUT TRACKING</div><h1>Plan unavailable</h1></div><RequestError error={plan.error} refresh={() => { void plan.refetch() }}/></section>

  const record = plan.data
  if (payouts.isPending) return <section className="discovery-page"><a className="back-link" href={`/allocation/${record.id}`}>← Allocation review</a><div className="discovery-heading"><div className="eyebrow">PAYOUT TRACKING</div><h1>Loading payout records…</h1></div><div className="skeleton session-skeleton"/></section>
  const batches = payouts.data?.batches ?? []
  const latest = [...batches].sort((a, b) => b.attempt - a.attempt)[0]
  const items = currentItems(batches)
  const currentState = currentStatus(record.state, batches, items)
  const attention = currentState === 'Needs attention'
  const payableRecipients = record.lines.reduce((count, line) => count + (line.maintainers.length || (line.payable_cents > 0 ? 1 : 0)), 0)
  const payoutRecipients = record.lines.flatMap((line) => line.maintainers.map((maintainer) => ({
    id: maintainer.maintainer_id,
    name: maintainer.display_name,
    project: line.display_name,
    amount_cents: maintainer.amount_cents,
  })))
  const recipientPreviewValid = payoutPreviewValid(record.payable_total_cents, payableRecipients,
    payoutRecipients)
  const recipientCount = batches.length ? items.length : payableRecipients
  const successful = items.filter((item) => item.status === 'success').length
  const pending = items.filter((item) => item.status === 'pending').length
  const failed = items.filter((item) => item.status === 'failed').length
  const needingAttention = items.filter((item) => item.status === 'needs_attention').length
  const failedInLatest = latest?.items.filter((item) => item.status === 'failed').length ?? 0
  const canRetry = record.state === 'settled_with_exceptions' && failedInLatest > 0
  const canReconcile = ['paying', 'settled_with_exceptions'].includes(record.state) && batches.length > 0
  const actionsPending = dispatch.isPending || retry.isPending || reconcile.isPending
  const error = dispatch.error || retry.error || reconcile.error || (payouts.isError ? payouts.error : null)

  return <section className="discovery-page payouts-page">
    <a className="back-link" href={`/allocation/${record.id}`}>← Allocation review</a>
    {record.synthetic_demo && <SyntheticDemoBanner/>}
    <div className="discovery-heading discovery-heading-session"><div><div className="eyebrow">PAYOUT TRACKING</div><h1>Distribution status.</h1><p>Batch and maintainer statuses from the payout service.</p></div><span className={`status-pill ${attention ? 'status-attention' : currentState === 'Successful' ? 'status-success' : currentState === 'Failed' ? 'status-failed' : ''}`}><span/>{currentState}</span></div>

    <section className="session-panel allocation-summary" aria-labelledby="payout-summary-title">
      <div className="session-panel-head"><div><span className="section-kicker">PLAN &amp; DISTRIBUTION</span><h2 id="payout-summary-title">Plan {record.id.slice(0, 8)}</h2></div><code className="session-id">{statusLabel(record.state)}</code></div>
      <div className="session-stats allocation-stats payout-stats">
        <Summary label="Total payable" value={money(record.payable_total_cents)}/>
        <Summary label={batches.length ? 'Payout recipients' : 'Eligible recipients'} value={recipientCount.toLocaleString()}/>
        <Summary label="Successful items" value={successful.toLocaleString()}/>
        <Summary label="Pending items" value={pending.toLocaleString()}/>
        <Summary label="Failed items" value={failed.toLocaleString()}/>
        <Summary label="Needs attention" value={needingAttention.toLocaleString()}/>
      </div>
      <p className="allocation-summary-note"><strong>{batches.length ? 'Item counts reflect the latest attempt for each upstream project.' : record.state === 'funded' ? 'Ready to distribute.' : record.state === 'paying' ? 'The plan is marked paying, but no batch details are available.' : 'No payout batch has been created.'}</strong>{!batches.length && record.state !== 'funded' && record.state !== 'paying' && ' Distribution is available after confirmed funding.'}</p>
    </section>

    {record.state === 'funded' && batches.length === 0 && <section className="continue-panel payout-action-panel" aria-labelledby="dispatch-title"><div className="continue-copy"><span className="section-kicker">READY TO DISTRIBUTE</span><h2 id="dispatch-title">Send payable allocations</h2><p>This sends {money(record.payable_total_cents)} through PayPal Sandbox to the opted-in recipients below. Review the exact split before confirming.</p>
      {recipientPreviewValid ? <ul className="maintainer-lines">{payoutRecipients.map((item) => <li key={item.id}><span>{item.name} · {item.project}</span><strong>{money(item.amount_cents)}</strong></li>)}</ul> : <p className="form-error" role="alert">The frozen recipient breakdown does not match the payable total. Payout submission is blocked.</p>}
    </div><div className="approval-actions"><button className="button button-primary" type="button" disabled={actionsPending || dispatch.isSuccess || !recipientPreviewValid} onClick={() => {
      if (window.confirm(payoutConfirmationMessage(record.payable_total_cents, payoutRecipients))) dispatch.mutate()
    }}>{dispatch.isPending ? 'Submitting payouts…' : 'Review and send payouts'}</button></div></section>}

    {dispatch.isSuccess && <p className="form-success" role="status">PayPal provider response: batch {dispatch.data.batch_id}; {dispatch.data.item_count} items; status {statusLabel(dispatch.data.state)}.</p>}

    {record.state === 'paying' && <section className={`payout-state-note${attention || !batches.length ? ' payout-state-attention' : ''}`} role="status"><strong>{!batches.length ? 'Payout outcome is uncertain.' : attention ? 'Provider outcome needs attention.' : 'Payouts are processing.'}</strong><span>{!batches.length ? 'Do not submit another batch. The API has no batch reference to reconcile yet; refresh status before taking further action.' : attention ? 'Do not submit another batch. Reconcile the existing batch to check provider status.' : 'The latest saved item statuses are shown below. Reconcile to request current provider results.'}</span></section>}

    {canReconcile && <section className="payout-reconcile-row"><p>{attention ? 'An uncertain or unrecognized result is held for provider reconciliation.' : 'Reconciliation checks the existing provider batch and updates item statuses.'}</p><button className="button button-secondary" type="button" disabled={actionsPending} onClick={() => reconcile.mutate()}>{reconcile.isPending ? 'Checking provider…' : 'Reconcile payout status'}</button></section>}
    {canRetry && <section className="continue-panel payout-action-panel" aria-labelledby="retry-title"><div className="continue-copy"><span className="section-kicker">CONFIRMED FAILED ITEMS</span><h2 id="retry-title">Retry {failedInLatest} failed payout{failedInLatest === 1 ? '' : 's'}</h2><p>Only PayPal-confirmed failed items are eligible. Pending and unknown items remain reserved and cannot be retried.</p></div><div className="approval-actions"><button className="button button-secondary" type="button" disabled={actionsPending} onClick={() => { if (window.confirm(`Retry only the ${failedInLatest} payout items currently confirmed failed?`)) retry.mutate() }}>{retry.isPending ? 'Submitting eligible retries…' : 'Review and retry failed items'}</button></div></section>}

    {error && <RequestError error={error} refresh={() => { dispatch.reset(); retry.reset(); reconcile.reset(); void Promise.all([plan.refetch(), payouts.refetch()]) }}/ >}

    <section className="source-section payout-batches" aria-labelledby="payout-batches-title">
      <div className="source-section-heading"><div><span className="section-kicker">PAYOUT HISTORY</span><h2 id="payout-batches-title">Batches &amp; recipients</h2></div><span className="row-count">{batches.length.toLocaleString()} batches</span></div>
      {payouts.isError && !error && <RequestError error={payouts.error} refresh={() => { void payouts.refetch() }}/ >}
      {!batches.length && <div className="empty-state"><h3>No payouts yet</h3><p>{record.state === 'funded' ? 'This funded plan is ready to distribute when you choose.' : `Current plan state is ${statusLabel(record.state)}. Payouts appear here after the backend records a batch.`}</p></div>}
      {batches.map((batch) => <Batch key={`${batch.attempt}-${batch.id}`} batch={batch} names={new Map(record.lines.map((line) => [line.upstream_key, line.display_name]))}/>)}
    </section>
    <p className="payout-data-note">Recipient email addresses are not returned. Provider item error details appear when PayPal supplies them.</p>
  </section>
}

function Batch({ batch, names }: { batch: PayoutBatch; names: Map<string, string> }) {
  return <section className="payout-batch" aria-label={`Payout batch attempt ${batch.attempt}`}>
    <div className="payout-batch-head"><div><span className="section-kicker">ATTEMPT {batch.attempt}</span><h3>Batch reference <code>{batch.id}</code></h3></div><span className={`status-pill ${statusClass(batch.status)}`}><span/>{statusLabel(batch.status)}</span></div>
    <div className="payout-batch-meta"><span>{batch.items.length.toLocaleString()} items</span><span>Actual batch fees: {batch.fees_cents === null ? 'Not reported' : money(batch.fees_cents)}</span></div>
    {!batch.items.length ? <p className="empty-batch">No payout items were returned for this batch.</p> : <div className="table-scroll payout-table-scroll" role="region" aria-label={`Payout items for attempt ${batch.attempt}`} tabIndex={0}>
      <table className="source-table payout-table"><thead><tr><th scope="col">Upstream project</th><th scope="col">Recipient</th><th scope="col">Amount</th><th scope="col">Item reference</th><th scope="col">Status</th><th scope="col">Updated</th></tr></thead>
        <tbody>{batch.items.map((item) => <PayoutRow key={item.sender_item_id} item={item} name={names.get(item.upstream_key) ?? item.upstream_key}/>)}</tbody>
      </table>
    </div>}
  </section>
}

function PayoutRow({ item, name }: { item: PayoutItem; name: string }) {
  return <tr><th scope="row" data-label="Upstream project"><strong>{name}</strong><code>{item.upstream_key}</code></th>
    <td data-label="Recipient">{item.recipient_display}</td><td data-label="Amount"><strong className="allocation-amount">{money(item.amount_cents)}</strong></td>
    <td data-label="Item reference"><code className="payout-item-id">{item.sender_item_id}</code></td>
    <td data-label="Status"><span className={`status-pill ${statusClass(item.status)}`}><span/>{statusLabel(item.status)}</span>{item.failure_reason && <small className="payout-error-note">{item.failure_reason}</small>}</td>
    <td data-label="Updated">{new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(item.updated_at))}</td></tr>
}

function RequestError({ error, refresh }: { error: Error; refresh: () => void }) {
  return <div className="discovery-error" role="alert"><span>{error.message}</span>{error instanceof ApiError && error.code && <code>{error.code}</code>}{error instanceof ApiError && error.requestId && <code>Request ID {error.requestId}</code>}<button className="text-button" type="button" onClick={refresh}>Refresh status</button></div>
}

function Summary({ label, value }: { label: string; value: string }) { return <div className="summary-item"><span>{label}</span><strong>{value}</strong></div> }
