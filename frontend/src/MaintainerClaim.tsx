import { useEffect, useState } from 'react'
import { ApiError, getMaintainerClaim, redeemMaintainerClaim, startMaintainerClaim, type MaintainerClaim as Claim } from './api'

const resultCopy: Record<string, string> = {
  denied: 'GitHub authorization was declined. You can try again when ready.',
  authorization_failed: 'GitHub could not verify this authorization. Start a new attempt.',
  authorization_revoked: 'GitHub authorization is no longer valid. Start a new attempt.',
  permissions_rejected: 'GitHub returned permissions Dividend does not need. The token was revoked and this account was not linked.',
  permissions_rejected_cleanup: 'GitHub returned permissions Dividend does not need. This account was not linked, but Dividend could not confirm token revocation.',
  account_conflict: 'This GitHub account is already linked to another maintainer claim. Contact Dividend support.',
  verified: 'Your GitHub account is verified for this maintainer claim.',
}

export default function MaintainerClaim() {
  const [claim, setClaim] = useState<Claim | null>(null)
  const [error, setError] = useState('')
  const [requestId, setRequestId] = useState('')
  const [result] = useState(() => new URLSearchParams(window.location.search).get('result') ?? '')
  const [busy, setBusy] = useState(true)

  useEffect(() => {
    const token = new URLSearchParams(window.location.hash.slice(1)).get('token')
    const load = token
      ? redeemMaintainerClaim(token).then((value) => {
          window.history.replaceState(null, '', '/claim')
          return value
        })
      : getMaintainerClaim()
    void load.then(setClaim).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : 'Claim status could not be loaded.')
      setRequestId(reason instanceof ApiError ? reason.requestId ?? '' : '')
    }).finally(() => setBusy(false))
  }, [])

  async function verify() {
    setBusy(true)
    setError('')
    setRequestId('')
    try {
      const { authorization_url } = await startMaintainerClaim()
      window.location.assign(authorization_url)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not start GitHub authorization.')
      setRequestId(reason instanceof ApiError ? reason.requestId ?? '' : '')
      setBusy(false)
    }
  }

  return <section className="claim-page" aria-labelledby="claim-title">
    <div className="eyebrow">MAINTAINER IDENTITY</div>
    <h1 id="claim-title">Verify your GitHub account.</h1>
    <p>Dividend uses GitHub’s authenticated account ID to verify this claim. It does not request private repository access.</p>
    {result && <p className={result === 'verified' ? 'claim-result claim-success' : 'claim-result'} role="status">{resultCopy[result] ?? 'GitHub authorization could not be completed.'}</p>}
    {(result === 'permissions_rejected' || result === 'permissions_rejected_cleanup') && <p><a href="https://github.com/settings/applications" target="_blank" rel="noreferrer">Review or revoke GitHub application access</a>, then retry with Dividend’s dedicated OAuth app.</p>}
    {busy && !claim && <p role="status">Checking claim status…</p>}
    {error && <div className="discovery-error" role="alert"><span>{error}</span>{requestId && <code>Request ID {requestId}</code>}<button className="text-button" type="button" onClick={() => window.location.reload()}>Retry</button></div>}
    {claim?.status === 'verified' && <div className="claim-status"><strong>Verified as @{claim.github_login}</strong><span>GitHub account ID {claim.github_user_id}</span><small>Claim started {new Date(claim.created_at).toLocaleString()}</small><small>Verified {claim.verified_at ? new Date(claim.verified_at).toLocaleString() : 'successfully'}</small></div>}
    {claim && claim.status !== 'verified' && <button className="button button-primary" type="button" disabled={busy} onClick={() => void verify()}>{busy ? 'Connecting…' : 'Continue with GitHub'}</button>}
    {claim && <p className="claim-privacy">Only your public account identity is used. Dividend does not store the GitHub access token.</p>}
  </section>
}
