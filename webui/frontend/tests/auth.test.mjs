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

test('protected layout starts realtime work when authentication finishes after mount', async () => {
  const layout = await readFile(new URL('../src/layouts/AdminLayout.vue', import.meta.url), 'utf8')

  assert.match(layout, /watch\(\(\)\s*=>\s*auth\.user\?\.id/)
  assert.match(layout, /if \(userId\).*startBackground|userId\s*\?\s*startBackground/s)
  assert.match(layout, /statsStore\.startPolling\(\)/)
  assert.match(layout, /runtime\.connectAutoStream\(\)/)
  assert.doesNotMatch(layout, /onMounted\(\(\)\s*=>\s*{[^}]*if \(!auth\.user\) return/s)
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

test('tenant browser storage fails closed for missing identities and storage errors', async () => {
  const { readTenantStorage, tenantStorageKey } = await import('../src/stores/tenant-storage.js')
  const alice = { id: 'alice-id', role: 'admin' }

  assert.equal(tenantStorageKey('settings', ''), 'settings:anonymous')
  assert.equal(readTenantStorage(null, 'settings', alice), null)
  assert.equal(readTenantStorage({ getItem: () => null }, 'settings', null), null)

  const existing = new Map([[tenantStorageKey('settings', alice.id), 'tenant-value']])
  const existingStorage = {
    getItem: (key) => existing.has(key) ? existing.get(key) : null,
    setItem: (key, value) => existing.set(key, String(value)),
  }
  assert.equal(readTenantStorage(existingStorage, 'settings', alice), 'tenant-value')
  assert.equal(readTenantStorage(existingStorage, 'missing', { id: 'user-id', role: 'user' }), null)
  assert.equal(readTenantStorage(existingStorage, 'missing', alice), null)

  const sameOwner = new Map([
    ['legacy-settings', 'legacy-value'],
    ['legacy-settings:legacy-owner', alice.id],
  ])
  const sameOwnerStorage = {
    getItem: (key) => sameOwner.has(key) ? sameOwner.get(key) : null,
    setItem: (key, value) => sameOwner.set(key, String(value)),
  }
  assert.equal(readTenantStorage(sameOwnerStorage, 'legacy-settings', alice), 'legacy-value')

  const brokenStorage = { getItem: () => { throw new Error('storage unavailable') } }
  assert.equal(readTenantStorage(brokenStorage, 'settings', alice), null)
})
