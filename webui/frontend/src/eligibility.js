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

export function formatGCashDetail(check, formatTime = (value) => String(value)) {
  if (!check) return ''
  const parts = []
  if (check.decision) parts.push(`Decision: ${check.decision}`)
  parts.push(`Method: ${gcashDisplayLabel(check)}`)
  if (check.checkout_country || check.currency) {
    parts.push(`Checkout: ${[check.checkout_country, check.currency].filter(Boolean).join(' / ')}`)
  }
  if (check.checked_at) parts.push(`Checked: ${formatTime(check.checked_at)}`)
  return parts.join(' · ')
}
