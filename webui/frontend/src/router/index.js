import { createRouter, createWebHashHistory } from 'vue-router'
import NProgress from 'nprogress'
import { useAuthStore } from '@/stores/auth'

NProgress.configure({ showSpinner: false, trickleSpeed: 120, minimum: 0.15 })

// Hash routing avoids requiring an SPA fallback from FastAPI or a future Gin backend.
const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/Login.vue'),
    meta: { title: 'Sign in', public: true },
  },
  {
    path: '/setup',
    name: 'setup',
    component: () => import('@/views/Setup.vue'),
    meta: { title: 'Initial setup', public: true },
  },
  {
    path: '/',
    name: 'dashboard',
    component: () => import('@/views/Dashboard.vue'),
    meta: { title: 'Dashboard', icon: 'Odometer', group: 'Overview', requiresAuth: true },
  },
  {
    path: '/import',
    name: 'import',
    component: () => import('@/views/Import.vue'),
    meta: { title: 'Import mailboxes', icon: 'Upload', group: 'Registration', requiresAuth: true },
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('@/views/Register.vue'),
    meta: { title: 'Single registration', icon: 'VideoPlay', group: 'Registration', requiresAuth: true },
  },
  {
    path: '/auto',
    name: 'auto',
    component: () => import('@/views/AutoLoop.vue'),
    meta: { title: 'Automatic batch', icon: 'MagicStick', group: 'Registration', requiresAuth: true },
  },
  {
    path: '/proxy',
    name: 'proxy',
    component: () => import('@/views/ProxyPool.vue'),
    meta: { title: 'Proxy pool', icon: 'Connection', group: 'Registration', requiresAuth: true },
  },
  {
    path: '/pool',
    name: 'pool',
    component: () => import('@/views/Pool.vue'),
    meta: { title: 'Mailbox pool', icon: 'Files', group: 'Data', requiresAuth: true },
  },
  {
    path: '/registered',
    name: 'registered',
    component: () => import('@/views/Registered.vue'),
    meta: { title: 'Registered accounts', icon: 'CircleCheck', group: 'Data', requiresAuth: true },
  },
  {
    path: '/runs',
    name: 'runs',
    component: () => import('@/views/Runs.vue'),
    meta: { title: 'Runs', icon: 'Document', group: 'Data', requiresAuth: true },
  },
  {
    path: '/settings/mail',
    name: 'mail',
    component: () => import('@/views/MailConfig.vue'),
    meta: { title: 'Mailbox settings', icon: 'Message', group: 'Settings', requiresAuth: true },
  },
  {
    path: '/settings/sms',
    name: 'sms',
    component: () => import('@/views/SmsConfig.vue'),
    meta: { title: 'SMS settings', icon: 'Iphone', group: 'Settings', requiresAuth: true },
  },
  {
    path: '/settings/export',
    name: 'export',
    component: () => import('@/views/ExportConfig.vue'),
    meta: { title: 'Export settings', icon: 'Share', group: 'Settings', requiresAuth: true },
  },
  {
    path: '/admin/users',
    name: 'admin-users',
    component: () => import('@/views/AdminUsers.vue'),
    meta: { title: 'User management', icon: 'User', group: 'Administration', requiresAuth: true, requiresAdmin: true },
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

// Top progress bar for route changes.
router.beforeEach((to, from, next) => {
  NProgress.start()
  if (to.meta?.title) document.title = `${to.meta.title} · GPT Auto Register`
  const auth = useAuthStore()
  if (!auth.ready) {
    auth.load().then(() => {
      if (to.meta?.requiresAuth && !auth.user) return next({ name: auth.setupRequired ? 'setup' : 'login', query: { redirect: to.fullPath } })
      if (to.meta?.requiresAdmin && auth.user?.role !== 'admin') return next({ name: 'dashboard' })
      next()
    }).catch(() => next({ name: 'login' }))
    return
  }
  if (to.meta?.requiresAuth && !auth.user) return next({ name: auth.setupRequired ? 'setup' : 'login', query: { redirect: to.fullPath } })
  if (to.meta?.requiresAdmin && auth.user?.role !== 'admin') return next({ name: 'dashboard' })
  next()
})
router.afterEach(() => {
  NProgress.done()
})

export default router
