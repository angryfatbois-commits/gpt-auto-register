import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

test('frontend has a login route and does not persist the session token in localStorage', async () => {
  const router = await readFile(new URL('../src/router/index.js', import.meta.url), 'utf8')
  const request = await readFile(new URL('../src/api/request.js', import.meta.url), 'utf8')
  assert.match(router, /\/login/)
  assert.match(router, /requiresAuth|auth/)
  assert.doesNotMatch(request, /localStorage\.(?:setItem|getItem).*session/i)
})

test('admin users view exposes create-user controls in English', async () => {
  const view = await readFile(new URL('../src/views/AdminUsers.vue', import.meta.url), 'utf8')
  assert.match(view, /Create user/i)
  assert.match(view, /Username/i)
  assert.match(view, /Password/i)
})

test('protected layout disconnects tenant background work when it unmounts', async () => {
  const layout = await readFile(new URL('../src/layouts/AdminLayout.vue', import.meta.url), 'utf8')
  const runtime = await readFile(new URL('../src/stores/runtime.js', import.meta.url), 'utf8')

  assert.match(layout, /onUnmounted/)
  assert.match(layout, /statsStore\.stopPolling\(\)/)
  assert.match(layout, /runtime\.disconnectStreams\(\)/)
  assert.match(runtime, /function disconnectStreams\(/)
  assert.match(runtime, /clearTimeout\(autoReconnectTimer\)/)
})

test('legacy browser state can be claimed by only one administrator', async () => {
  const { readTenantStorage, tenantStorageKey } = await import('../src/stores/tenant-storage.js')
  const values = new Map([['legacy-settings', '{"proxy":"old"}']])
  const storage = {
    getItem: (key) => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => values.set(key, String(value)),
  }

  const alice = { id: 'alice-id', role: 'admin' }
  const bob = { id: 'bob-id', role: 'admin' }
  assert.equal(readTenantStorage(storage, 'legacy-settings', alice), '{"proxy":"old"}')
  assert.equal(values.get(tenantStorageKey('legacy-settings', alice.id)), '{"proxy":"old"}')
  assert.equal(readTenantStorage(storage, 'legacy-settings', bob), null)
})
