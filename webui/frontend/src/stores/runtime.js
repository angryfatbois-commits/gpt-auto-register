import { defineStore } from 'pinia'
import { ref } from 'vue'
import { createSSE } from '@/api/request'
import { useStatsStore } from './stats'

let _logId = 0
const MAX_LOGS = 2000

const legacyMarker = (...codePoints) => String.fromCodePoint(...codePoints)
const LEGACY_ERROR_MARKERS = [
  legacyMarker(0x5931, 0x8d25),
  legacyMarker(0x62d2, 0x7edd),
]
const LEGACY_SUCCESS_MARKERS = [
  legacyMarker(0x6210, 0x529f),
  legacyMarker(0x5b8c, 0x6210),
  legacyMarker(0x547d, 0x4e2d),
]

function classify(line) {
  const l = (line || '').toLowerCase()
  // Preserve legacy log classification without shipping non-English string literals.
  if (l.includes('error') || l.includes('failed') || l.includes('rejected') || LEGACY_ERROR_MARKERS.some((marker) => l.includes(marker))) return 'err'
  if (l.includes('warning') || l.includes('warn')) return 'warn'
  if (l.includes('success') || l.includes('complete') || l.includes('matched') || l.includes('ok') || LEGACY_SUCCESS_MARKERS.some((marker) => l.includes(marker))) return 'ok'
  return ''
}

// Runtime state: global live log, single-run SSE, automatic-run SSE/status, and alert banner.
// Keeping it in a store prevents background runs and logs from stopping during navigation.
export const useRuntimeStore = defineStore('runtime', () => {
  const logs = ref([])            // { id, text, kind }
  const autoStatus = ref({ state: 'stopped', registered_ok: 0, registered_fail: 0 })
  const banner = ref('')          // Circuit-breaker or critical-error banner.
  const lastRunResult = ref(null) // { email, password, access_token_len, partial } or { error }.
  const dataVersion = ref(0)      // Increment to refresh pool, result, and run tables.
  const runningSingle = ref(false)

  let manualEs = null
  let autoEs = null
  let autoReconnectTimer = null
  let autoStreamWanted = false

  function addLog(text, kind) {
    logs.value.push({ id: ++_logId, text, kind: kind ?? classify(text) })
    if (logs.value.length > MAX_LOGS) logs.value.splice(0, logs.value.length - MAX_LOGS)
  }
  function clearLogs() { logs.value = [] }
  function bumpData() { dataVersion.value++ }
  function dismissBanner() { banner.value = '' }

  function finalizeRunStream(es) {
    if (!es || es.__finalized) return
    es.__finalized = true
    try { es.close() } catch (_) {}

    if (manualEs === es) {
      manualEs = null
      runningSingle.value = false
    }

    // The database write happens before the run stream ends. Reconcile both
    // statistics and cached tables even when EventSource ended through error.
    useStatsStore().refresh()
    bumpData()
  }

  function handleRunStatus(d, automatic = false) {
    if (d.kind === 'done') {
      if (!automatic) {
        lastRunResult.value = {
          email: d.email,
          password: d.password || '',
          access_token_len: d.access_token_len,
          totp_secret: d.totp_secret || '',
          partial: d.partial,
        }
      }
      addLog(
        `Registration complete: ${d.email}${d.password ? ' / ' + d.password : ''}`
        + ` (access_token=${d.access_token_len}${d.partial ? ', partial credentials' : ''})`,
        'ok',
      )
    } else if (d.kind === 'error') {
      if (!automatic) lastRunResult.value = { email: d.email, error: d.message }
      addLog('Error: ' + d.message, 'err')
    } else if (d.kind === 'phase') {
      addLog(`phase=${d.phase} email=${d.email}`, 'evt')
    }
    if (automatic && (d.kind === 'done' || d.kind === 'error')) {
      useStatsStore().refresh()
      bumpData()
    }
  }

  function openRunStream(runId) {
    const id = String(runId || '').trim()
    if (!id) return null

    if (manualEs) {
      manualEs.__finalized = true
      try { manualEs.close() } catch (_) {}
      manualEs = null
    }
    runningSingle.value = true
    // A manual registration claims the mailbox before returning its run ID.
    // Refresh now so the header immediately moves it to In progress.
    useStatsStore().refresh()
    bumpData()

    let es = null
    es = createSSE(`/api/runs/${id}/stream`, {
      log: (e) => {
        try {
          const d = JSON.parse(e.data)
          if (d.line) addLog(d.line)
        } catch (_) {}
      },
      status: (e) => {
        try {
          handleRunStatus(JSON.parse(e.data))
        } catch (_) {}
      },
      end: () => {
        finalizeRunStream(es)
      },
    }, () => {
      finalizeRunStream(es)
    })

    manualEs = es
    return es
  }

  // ─── SSE for a registration run started manually ───
  function streamRun(runId) {
    return openRunStream(runId)
  }

  // ─── Global automatic-run SSE, connected at startup with automatic reconnect ───
  function connectAutoStream() {
    autoStreamWanted = true
    if (autoReconnectTimer) {
      clearTimeout(autoReconnectTimer)
      autoReconnectTimer = null
    }
    if (autoEs) { try { autoEs.close() } catch (_) {} }
    const es = createSSE('/api/auto/stream', {
      state: (e) => {
        try {
          const next = JSON.parse(e.data)
          autoStatus.value = next

          const statsStore = useStatsStore()
          if (next.pool_stats && statsStore.applySnapshot(next.pool_stats)) bumpData()

        } catch (_) {}
      },
      run_started: (e) => {
        try {
          const d = JSON.parse(e.data)
          addLog(`[auto] Registration started for ${d.email} (run=${d.run_id})`, 'evt')
        } catch (_) {}
      },
      run_event: (e) => {
        try {
          const d = JSON.parse(e.data)
          const payload = d.data || {}
          if (d.event === 'log' && payload.line) addLog(payload.line)
          else if (d.event === 'status') handleRunStatus(payload, true)
        } catch (_) {}
      },
      run_finished: (e) => {
        try {
          const d = JSON.parse(e.data)
          const tag = d.ok ? '[success]' : (d.category === 'network' ? '[network error; account released]' : '[failed]')
          addLog(`[auto] ${tag} ${d.email} finished`, d.ok ? 'ok' : 'err')
          useStatsStore().refresh()
          bumpData()
        } catch (_) {}
      },
      circuit_break: (e) => {
        try {
          const d = JSON.parse(e.data)
          addLog(`[auto] Circuit breaker: ${d.reason}`, 'err')
          banner.value = d.reason
        } catch (_) {}
      },
    }, () => {
      // Reconnect automatically after a disconnect.
      try { es.close() } catch (_) {}
      if (autoEs !== es || !autoStreamWanted) return
      autoEs = null
      autoReconnectTimer = setTimeout(() => {
        autoReconnectTimer = null
        if (autoStreamWanted) connectAutoStream()
      }, 2000)
    })
    autoEs = es
  }

  function disconnectStreams() {
    // Flip the intent before closing EventSource objects. Some browsers emit
    // an error event synchronously from close(), and that event must not
    // schedule a new connection after logout/unmount.
    autoStreamWanted = false
    if (autoReconnectTimer) {
      clearTimeout(autoReconnectTimer)
      autoReconnectTimer = null
    }
    if (manualEs) {
      manualEs.__finalized = true
      try { manualEs.close() } catch (_) {}
      manualEs = null
    }
    if (autoEs) {
      try { autoEs.close() } catch (_) {}
      autoEs = null
    }
    runningSingle.value = false
  }

  return {
    logs, autoStatus, banner, lastRunResult, dataVersion, runningSingle,
    addLog, clearLogs, bumpData, dismissBanner, streamRun, connectAutoStream, disconnectStreams,
  }
})
