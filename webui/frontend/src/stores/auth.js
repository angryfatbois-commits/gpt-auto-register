import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  createUser,
  disableUser,
  getCurrentUser,
  getSetupStatus,
  listUsers,
  login as loginRequest,
  logout as logoutRequest,
  resetUserPassword,
  setupAdmin,
} from '@/api/auth'

export const useAuthStore = defineStore('auth', () => {
  const user = ref(null)
  const ready = ref(false)
  const setupRequired = ref(false)

  async function load() {
    try {
      const result = await getCurrentUser()
      user.value = result.user || null
    } catch (error) {
      user.value = null
      try {
        const status = await getSetupStatus()
        setupRequired.value = Boolean(status.setup_required)
      } catch (_) {
        setupRequired.value = false
      }
    } finally {
      ready.value = true
    }
    return user.value
  }

  async function login(username, password) {
    const result = await loginRequest(username, password)
    user.value = result.user || null
    setupRequired.value = false
    ready.value = true
    return user.value
  }

  async function setup(username, password) {
    const result = await setupAdmin(username, password)
    user.value = result.user || null
    setupRequired.value = false
    ready.value = true
    return user.value
  }

  async function logout() {
    try { await logoutRequest() } finally { user.value = null }
  }

  return {
    user,
    ready,
    setupRequired,
    load,
    login,
    setup,
    logout,
    listUsers,
    createUser,
    disableUser,
    resetUserPassword,
  }
})
