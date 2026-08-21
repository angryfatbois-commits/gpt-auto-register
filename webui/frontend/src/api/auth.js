import http from './request'

export const login = (username, password) =>
  http.post('/api/auth/login', { username, password })

export const setupAdmin = (username, password) =>
  http.post('/api/auth/setup', { username, password })

export const getCurrentUser = () => http.get('/api/auth/me')
export const getSetupStatus = () => http.get('/api/auth/setup-status')
export const logout = () => http.post('/api/auth/logout')

export const listUsers = () => http.get('/api/admin/users')
export const createUser = (payload) => http.post('/api/admin/users', payload)
export const disableUser = (id) => http.delete(`/api/admin/users/${encodeURIComponent(id)}`)
export const resetUserPassword = (id, password) =>
  http.post(`/api/admin/users/${encodeURIComponent(id)}/password`, { password })
