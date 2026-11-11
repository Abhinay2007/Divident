import test from 'node:test'
import assert from 'node:assert/strict'
import { evidenceReviewPresentation, requestEvidenceReview } from '../src/evidenceReview.js'

test('AI review action calls the plan evidence-review endpoint with its access header', async () => {
  const calls = []
  const result = { status: 'available', summary: 'Reviewed PR #42.', cited_pr_numbers: [42] }
  const response = await requestEvidenceReview('plan/with space', { 'X-Plan-Token': 'capability' },
    async (url, options) => { calls.push([url, options]); return result })

  assert.equal(response, result)
  assert.deepEqual(calls, [[
    '/api/plans/plan%2Fwith%20space/evidence-review',
    { headers: { 'X-Plan-Token': 'capability' } },
  ]])
})

test('available AI response is presented with validated GitHub PR links', () => {
  const view = evidenceReviewPresentation({
    status: 'available', summary: 'Reviewed PR #42.',
    contributors: [{ contributor_id: '7', observation: 'Reviewed PR #42.', cited_pr_numbers: [42] }],
    cited_pr_numbers: [42], citations: [{ pr_number: 42, url: 'https://github.com/curl/curl/pull/42' },
      { pr_number: 42, url: 'https://evil.example/pull/42' }],
    advisory_flags: ['review_activity'], model: 'deepseek-ai/DeepSeek-V4.1-Flash-1017',
  })

  assert.equal(view.status, 'available')
  assert.equal(view.summary, 'Reviewed PR #42.')
  assert.deepEqual(view.citedPrNumbers, [42])
  assert.deepEqual(view.citations, [{ pr_number: 42, url: 'https://github.com/curl/curl/pull/42' }])
})

test('unavailable and invalid AI responses stay distinct from deterministic explanations', () => {
  assert.deepEqual(evidenceReviewPresentation({ status: 'unavailable', reason: 'modal_endpoint_unavailable' }), {
    status: 'unavailable', reason: 'modal_endpoint_unavailable',
  })
  assert.deepEqual(evidenceReviewPresentation({ status: 'invalid', reason: 'invalid_model_output' }), {
    status: 'invalid', reason: 'invalid_model_output',
  })
})
