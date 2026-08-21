import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

import {
  formatGCashDetail,
  gcashDisplayLabel,
  gcashProbeRequestConfig,
  gcashStatusType,
  summarizeGCash,
} from '../src/eligibility.js'


test('GCash API calls carry the explicit side-effect acknowledgement', () => {
  assert.deepEqual(gcashProbeRequestConfig(), {
    headers: {
      'X-GCash-Probe-Confirmation': 'checkout-side-effects-acknowledged',
    },
    timeout: 3_600_000,
  })
})


test('summarizeGCash collapses every non-positive result to unavailable', () => {
  const summary = summarizeGCash({
    'one@example.com': { classification: 'eligible' },
    'two@example.com': { classification: 'ineligible' },
    'three@example.com': { classification: 'unknown' },
  })

  assert.deepEqual(summary.counts, { eligible: 1, ineligible: 2 })
  assert.equal(summary.text, 'Completed: 1 available, 2 unavailable')
})

test('failed legacy status detail is presented as unavailable', () => {
  const text = formatGCashDetail(
    {
      classification: 'unknown',
      decision: 'checkout_timeout',
      label: 'GCash check failed',
      checked_at: 100,
      last_conclusive: {
        classification: 'eligible',
        decision: 'gcash_zero_due_available',
      },
    },
    (value) => `time:${value}`,
  )

  assert.match(text, /Decision: checkout_timeout/)
  assert.match(text, /Method: GCash unavailable/)
  assert.match(text, /Checked: time:100/)
  assert.doesNotMatch(text, /unknown|check failed/i)
})

test('status colors are binary for available and unavailable checks', () => {
  assert.equal(gcashStatusType('eligible'), 'success')
  assert.equal(gcashStatusType('ineligible'), 'warning')
  assert.equal(gcashStatusType('unknown'), 'warning')
})

test('availability detail shows the inferred checkout region when present', () => {
  const text = formatGCashDetail(
    {
      classification: 'ineligible',
      decision: 'gcash_unavailable',
      checkout_country: 'US',
      currency: 'USD',
      checked_at: 100,
    },
    (value) => `time:${value}`,
  )

  assert.match(text, /Method: GCash unavailable/)
  assert.match(text, /Checkout: US \/ USD/)
})

test('availability detail exposes only sanitized capability diagnostics', () => {
  const text = formatGCashDetail(
    {
      classification: 'ineligible',
      decision: 'gcash_unavailable',
      custom_method_probe_status: 'failed',
      custom_method_probe_failure: 'stripe_custom_capability_transport_error',
      custom_method_probe_exception: 'ConnectionError',
    },
  )

  assert.match(text, /Capability: failed/)
  assert.match(text, /Issue: stripe_custom_capability_transport_error/)
  assert.match(text, /Exception: ConnectionError/)
  assert.doesNotMatch(text, /Bearer|cookie|secret|cpmt_/i)

  const unsafe = formatGCashDetail({
    decision: 'gcash_unavailable',
    custom_method_probe_failure: 'Bearer secret-token',
  })
  assert.doesNotMatch(unsafe, /Bearer|secret-token/i)
})

test('legacy stored labels are presented as availability labels', () => {
  assert.equal(gcashDisplayLabel({ label: 'GCash eligible' }), 'GCash available')
  assert.equal(gcashDisplayLabel({ label: 'GCash ineligible' }), 'GCash unavailable')
  assert.equal(gcashDisplayLabel({ label: 'GCash status unknown' }), 'GCash unavailable')
})

test('GCash confirmation explains the source-compatible PH workflow', async () => {
  const view = await readFile(new URL('../src/views/Registered.vue', import.meta.url), 'utf8')

  assert.match(view, /PH\/PHP checkout/)
  assert.match(view, /applies the Plus campaign/)
  assert.match(view, /synchronizes taxes/)
  assert.match(view, /Philippines proxy is required/)
  assert.doesNotMatch(view, /selected proxy\/IP country/)
})
