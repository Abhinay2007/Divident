import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')

test('synthetic warning is exact and persists across workflow screens', () => {
  const banner = read('../src/SyntheticDemoBanner.tsx')
  assert.match(banner, /SYNTHETIC SANDBOX DEMO — All contributors and contribution evidence in this workflow are fictional\. No real maintainer eligibility is established\./)
  for (const path of [
    '../src/Discovery.tsx', '../src/AllocationReview.tsx',
    '../src/FundScreen.tsx', '../src/Payouts.tsx',
  ]) {
    assert.match(read(path), /record\.synthetic_demo && <SyntheticDemoBanner\/>/)
  }
  const activity = read('../src/WorkspaceActivity.tsx')
  assert.match(activity, /SYNTHETIC SANDBOX DEMO/)
})

test('home entry point is labelled exactly as a synthetic Sandbox demo', () => {
  assert.match(read('../src/App.tsx'), /Create Synthetic Sandbox Demo/)
})
