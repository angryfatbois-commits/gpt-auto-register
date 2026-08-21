import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getStats } from '@/api/accounts'

const STAT_KEYS = ['total', 'available', 'in_use', 'done', 'failed']

function normalizeSnapshot(snapshot) {
  if (!snapshot || typeof snapshot !== 'object') return null
  return Object.fromEntries(STAT_KEYS.map((key) => {
    const value = Number(snapshot[key])
    return [key, Number.isFinite(value) && value >= 0 ? value : 0]
  }))
}

function snapshotsEqual(left, right) {
  return STAT_KEYS.every((key) => left[key] === right[key])
}

// Account-pool totals shared by the header and dashboard. SSE snapshots update
// them immediately; polling remains a fallback for manual or missed changes.
export const useStatsStore = defineStore('stats', () => {
  const stats = ref({ total: 0, available: 0, in_use: 0, done: 0, failed: 0 })
  let timer = null
  let revision = 0

  function applySnapshot(snapshot) {
    const next = normalizeSnapshot(snapshot)
    if (!next) return false
    const changed = !snapshotsEqual(stats.value, next)
    if (changed) stats.value = next
    // A valid realtime snapshot is newer than any HTTP request that started
    // before it, even when its numeric values happen to be unchanged.
    revision++
    return changed
  }

  async function refresh() {
    const startingRevision = revision
    try {
      const { stats: s } = await getStats()
      // Do not let a slow polling response overwrite a newer SSE snapshot.
      if (s && revision === startingRevision) applySnapshot(s)
    } catch (e) {
      // Keep polling failures silent so they do not interrupt the user.
      console.error('stats refresh:', e)
    }
  }

  function startPolling(interval = 5000) {
    refresh()
    if (timer) clearInterval(timer)
    timer = setInterval(refresh, interval)
  }

  function stopPolling() {
    if (timer) clearInterval(timer)
    timer = null
  }

  return { stats, applySnapshot, refresh, startPolling, stopPolling }
})
