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
