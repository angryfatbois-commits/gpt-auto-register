import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'


function message(data) {
  return { data: JSON.stringify(data) }
}


async function loadRuntimeHarness() {
  const connections = []
  const stats = { value: { total: 0, available: 0, in_use: 0, done: 0, failed: 0 } }
  const statsStore = {
    stats,
    refreshCalls: 0,
    refresh() {
      this.refreshCalls += 1
      return Promise.resolve()
    },
    applySnapshot(next) {
      const changed = JSON.stringify(stats.value) !== JSON.stringify(next)
      stats.value = { ...next }
      return changed
    },
  }
  const dependencyKey = `__runtime_sync_${Date.now()}_${Math.random()}`
  globalThis[dependencyKey] = {
    defineStore: (_name, setup) => setup,
    ref: (value) => ({ value }),
    useStatsStore: () => statsStore,
    createSSE(path, handlers, onError) {
      const connection = {
        path,
        handlers,
        onError,
        closed: false,
        close() { this.closed = true },
      }
      connections.push(connection)
      return connection
    },
  }

  let source = await readFile(new URL('../src/stores/runtime.js', import.meta.url), 'utf8')
  source = source
    .replace(/^import[^\n]*\r?\n/gm, '')
    .replace('export const useRuntimeStore', 'const useRuntimeStore')
  const executable = [
    `const { defineStore, ref, createSSE, useStatsStore } = globalThis[${JSON.stringify(dependencyKey)}]`,
    source,
    'export { useRuntimeStore }',
  ].join('\n')

  try {
    const encoded = Buffer.from(executable).toString('base64')
    const module = await import(`data:text/javascript;base64,${encoded}#${Math.random()}`)
    return { store: module.useRuntimeStore(), connections, statsStore }
  } finally {
    delete globalThis[dependencyKey]
  }
}


async function loadStatsHarness() {
  let resolveRequest
  const dependencyKey = `__stats_sync_${Date.now()}_${Math.random()}`
  globalThis[dependencyKey] = {
    defineStore: (_name, setup) => setup,
    ref: (value) => ({ value }),
    getStats: () => new Promise((resolve) => { resolveRequest = resolve }),
  }

  let source = await readFile(new URL('../src/stores/stats.js', import.meta.url), 'utf8')
  source = source
    .replace(/^import[^\n]*\r?\n/gm, '')
    .replace('export const useStatsStore', 'const useStatsStore')
  const executable = [
    `const { defineStore, ref, getStats } = globalThis[${JSON.stringify(dependencyKey)}]`,
    source,
    'export { useStatsStore }',
  ].join('\n')

  try {
    const encoded = Buffer.from(executable).toString('base64')
    const module = await import(`data:text/javascript;base64,${encoded}#${Math.random()}`)
    return {
      store: module.useStatsStore(),
      resolveRequest: (value) => resolveRequest(value),
    }
  } finally {
    delete globalThis[dependencyKey]
  }
}


test('automatic workers keep independent live-log streams', async () => {
  const { store, connections } = await loadRuntimeHarness()
  store.connectAutoStream()
  const automatic = connections[0]

  automatic.handlers.run_started(message({ run_id: 'run-one', email: 'one@example.com' }))
  const first = connections.find((item) => item.path.includes('run-one'))
  automatic.handlers.run_started(message({ run_id: 'run-two', email: 'two@example.com' }))
  const second = connections.find((item) => item.path.includes('run-two'))

  assert.ok(first)
  assert.ok(second)
  assert.equal(first.closed, false, 'starting worker two must not close worker one')
  assert.equal(second.closed, false)
  assert.equal(store.runningSingle.value, false, 'automatic workers must not change manual-run state')

  first.handlers.log(message({ line: 'worker one live line' }))
  second.handlers.log(message({ line: 'worker two live line' }))
  assert.ok(store.logs.value.some((entry) => entry.text === 'worker one live line'))
  assert.ok(store.logs.value.some((entry) => entry.text === 'worker two live line'))
})


test('automatic state snapshots immediately reconcile pool statistics and cached views', async () => {
  const { store, connections, statsStore } = await loadRuntimeHarness()
  store.connectAutoStream()
  const automatic = connections[0]
  const snapshot = { total: 8, available: 5, in_use: 2, done: 1, failed: 0 }

  automatic.handlers.state(message({ state: 'running', pool_stats: snapshot, workers: [] }))

  assert.deepEqual(statsStore.stats.value, snapshot)
  assert.equal(store.dataVersion.value, 1)
})


test('a reconnect recovers live logs for workers already present in the state snapshot', async () => {
  const { store, connections } = await loadRuntimeHarness()
  store.connectAutoStream()
  const automatic = connections[0]

  automatic.handlers.state(message({
    state: 'running',
    pool_stats: { total: 1, available: 0, in_use: 1, done: 0, failed: 0 },
    workers: [{ run_id: 'already-running', email: 'active@example.com' }],
  }))

  assert.ok(
    connections.some((item) => item.path === '/api/runs/already-running/stream'),
    'the state snapshot must recover a run_started event missed before connection',
  )
})


test('an automatic run stream error performs a final data reconciliation', async () => {
  const { store, connections, statsStore } = await loadRuntimeHarness()
  store.connectAutoStream()
  const automatic = connections[0]
  automatic.handlers.run_started(message({ run_id: 'run-error', email: 'error@example.com' }))
  const run = connections.find((item) => item.path.includes('run-error'))
  const versionBeforeError = store.dataVersion.value

  run.onError(new Event('error'))

  assert.equal(run.closed, true)
  assert.equal(statsStore.refreshCalls, 1)
  assert.equal(store.dataVersion.value, versionBeforeError + 1)
  assert.equal(store.runningSingle.value, false)
})


test('a realtime snapshot cannot be overwritten by an older polling response', async () => {
  const { store, resolveRequest } = await loadStatsHarness()
  const pendingRefresh = store.refresh()
  const realtime = { total: 3, available: 1, in_use: 1, done: 1, failed: 0 }

  store.applySnapshot(realtime)
  resolveRequest({
    stats: { total: 2, available: 2, in_use: 0, done: 0, failed: 0 },
  })
  await pendingRefresh

  assert.deepEqual(store.stats.value, realtime)
})


test('successful import applies its response snapshot and invalidates the mailbox pool', async () => {
  const view = await readFile(new URL('../src/views/Import.vue', import.meta.url), 'utf8')

  assert.match(view, /statsStore\.applySnapshot\(r\.stats\)/)
  assert.match(view, /runtime\.bumpData\(\)/)
})


test('cached pool, registered-account, and run views subscribe to data invalidation', async () => {
  for (const name of ['Pool.vue', 'Registered.vue', 'Runs.vue']) {
    const view = await readFile(new URL(`../src/views/${name}`, import.meta.url), 'utf8')
    assert.match(view, /watch\(dataVersion,\s*\(\)\s*=>\s*load\(\)\)/, name)
    assert.match(view, /onActivated\(\(\)\s*=>\s*load\(\)\)/, name)
  }
})
