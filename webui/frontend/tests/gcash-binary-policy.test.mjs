import assert from 'node:assert/strict'
import test from 'node:test'

import {
  formatGCashDetail,
  gcashDisplayLabel,
  gcashStatusType,
  summarizeGCash,
} from '../src/eligibility.js'


test('GCash summaries expose only available and unavailable outcomes', () => {
  const summary = summarizeGCash({
    'available@example.com': { classification: 'eligible' },
    'legacy@example.com': { classification: 'unknown' },
    'malformed@example.com': {},
  })

  assert.deepEqual(summary.counts, { eligible: 1, ineligible: 2 })
  assert.equal(summary.text, 'Completed: 1 available, 2 unavailable')
})

test('legacy and failed GCash checks render as unavailable', () => {
  const legacy = {
    classification: 'unknown',
    label: 'GCash status unknown',
    decision: 'checkout_transport_error',
  }

  assert.equal(gcashDisplayLabel(legacy), 'GCash unavailable')
  assert.equal(gcashStatusType(legacy.classification), 'warning')
  assert.match(formatGCashDetail(legacy), /Method: GCash unavailable/)
  assert.doesNotMatch(formatGCashDetail(legacy), /unknown|check failed/i)
})
