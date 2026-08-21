export function gcashStatusType(classification) {
  return classification === 'eligible' ? 'success' : 'warning'
}

export function gcashProbeRequestConfig() {
  return {
    headers: {
      'X-GCash-Probe-Confirmation': 'checkout-side-effects-acknowledged',
    },
    timeout: 3_600_000,
  }
}

export function summarizeGCash(results) {
  const counts = { eligible: 0, ineligible: 0 }
  for (const result of Object.values(results || {})) {
    counts[result?.classification === 'eligible' ? 'eligible' : 'ineligible'] += 1
  }
  return {
    counts,
    text: `Completed: ${counts.eligible} available, ${counts.ineligible} unavailable`,
  }
}

export function gcashDisplayLabel(check) {
  if (!check) return ''
  return check.classification === 'eligible' || check.eligible === true ||
    check.method_available === true || check.label === 'GCash eligible' ||
    check.label === 'GCash available'
    ? 'GCash available'
    : 'GCash unavailable'
}

function diagnosticCode(value) {
  const text = String(value ?? '').trim()
  if (/^(?:cpmt_|bearer\b|cookie\b|secret\b)/i.test(text)) return ''
  return /^[A-Za-z][A-Za-z0-9_.-]{0,119}$/.test(text) ? text : ''
}

export function formatGCashDetail(check, formatTime = (value) => String(value)) {
  if (!check) return ''
  const parts = []
  const decision = diagnosticCode(check.decision)
  if (decision) parts.push(`Decision: ${decision}`)
  parts.push(`Method: ${gcashDisplayLabel(check)}`)
  const probeStatus = diagnosticCode(check.custom_method_probe_status)
  const probeFailure = diagnosticCode(check.custom_method_probe_failure)
  const probeException = diagnosticCode(check.custom_method_probe_exception)
  if (probeStatus) parts.push(`Capability: ${probeStatus}`)
  if (probeFailure) parts.push(`Issue: ${probeFailure}`)
  if (probeException) parts.push(`Exception: ${probeException}`)
  if (check.checkout_country || check.currency) {
    parts.push(`Checkout: ${[check.checkout_country, check.currency].filter(Boolean).join(' / ')}`)
  }
  if (check.checked_at) parts.push(`Checked: ${formatTime(check.checked_at)}`)
  return parts.join(' · ')
}
