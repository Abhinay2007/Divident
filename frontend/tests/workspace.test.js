import test from 'node:test'
import assert from 'node:assert/strict'
import { continuePath, hasWorkspaceActivity } from '../src/workspace.js'

test('Continue routes workflows to the existing step without creating a new record', () => {
  assert.equal(continuePath('draft', 'p1'), '/allocation/p1')
  assert.equal(continuePath('funding', 'p1'), '/fund/p1')
  assert.equal(continuePath('funded', 'p1'), '/payouts/p1')
  assert.equal(continuePath('paying', 'p1'), '/payouts/p1')
  assert.equal(continuePath('settled', 'p1'), '/payouts/p1')
})

test('workspace distinguishes true empty state from persisted activity', () => {
  assert.equal(hasWorkspaceActivity([]), false)
  assert.equal(hasWorkspaceActivity([{ id: 'saved-session' }]), true)
})
