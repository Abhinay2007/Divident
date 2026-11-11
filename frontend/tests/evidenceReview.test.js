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
    contributors: [{ contributor_id: '7', observation: 'Reviewed one PR.', cited_pr_numbers: [42],
      merged_pr_count: 0, reviewed_pr_count: 1 }],
    cited_pr_numbers: [42], citations: [
      { contributor_id: '7', activity_type: 'code_review', pr_number: 42, url: 'https://github.com/curl/curl/pull/42' },
      { contributor_id: '7', activity_type: 'code_review', pr_number: 42, url: 'https://evil.example/pull/42' },
    ],
    advisory_flags: ['review_activity'], model: 'deepseek-ai/DeepSeek-V4.1-Flash-1017',
  })

  assert.equal(view.status, 'available')
  assert.equal(view.summary, 'Reviewed PR #42.')
  assert.deepEqual(view.citedPrNumbers, [42])
  assert.deepEqual(view.citations, [{ contributor_id: '7', activity_type: 'code_review', pr_number: 42, url: 'https://github.com/curl/curl/pull/42' }])
  assert.equal(view.contributorGroups[0].label, 'Review-focused participation')
  assert.equal(view.contributorGroups[0].contributors[0].reviewed_pr_count, 1)
})

test('contributor groups and source links distinguish merged, review-only, and mixed activity', () => {
  const view = evidenceReviewPresentation({
    status: 'available', summary: 'Contribution evidence across projects.',
    contributors: [
      { contributor_id: '7001', observation: 'Review participation.', cited_pr_numbers: [8], merged_pr_count: 0, reviewed_pr_count: 1 },
      { contributor_id: '7002', observation: 'A merged contribution.', cited_pr_numbers: [9], merged_pr_count: 1, reviewed_pr_count: 0 },
      { contributor_id: '7003', observation: 'Merged and reviewed work.', cited_pr_numbers: [10, 11], merged_pr_count: 1, reviewed_pr_count: 1 },
    ],
    citations: [
      { contributor_id: '7001', activity_type: 'code_review', pr_number: 8, url: 'https://github.com/a/b/pull/8' },
      { contributor_id: '7001', activity_type: 'code_review', pr_number: 8, url: 'https://github.com/a/b/pull/8' },
      { contributor_id: '7002', activity_type: 'merged_pull_request', pr_number: 9, url: 'https://github.com/a/c/pull/9' },
      { contributor_id: '7003', activity_type: 'merged_pull_request', pr_number: 10, url: 'https://github.com/a/d/pull/10' },
      { contributor_id: '7003', activity_type: 'code_review', pr_number: 11, url: 'https://github.com/a/d/pull/11' },
    ],
  })
  assert.deepEqual(view.contributorGroups.map((group) => group.label), [
    'Review-focused participation', 'Verified merged contribution', 'Merged work and reviews',
  ])
  assert.equal(view.contributorGroups[0].contributors[0].reviewed_pr_count, 1)
  assert.equal(view.contributorGroups[0].contributors[0].citations.length, 1)
  assert.equal(view.contributorGroups[1].contributors[0].merged_pr_count, 1)
  assert.deepEqual(view.contributorGroups[2].contributors[0].citations.map((item) => item.contributor_id), ['7003', '7003'])
})

test('unavailable and invalid AI responses stay distinct from deterministic explanations', () => {
  assert.deepEqual(evidenceReviewPresentation({ status: 'unavailable', reason: 'modal_endpoint_unavailable' }), {
    status: 'unavailable', reason: 'modal_endpoint_unavailable',
  })
  assert.deepEqual(evidenceReviewPresentation({ status: 'invalid', reason: 'invalid_model_output' }), {
    status: 'invalid', reason: 'invalid_model_output',
  })
})
