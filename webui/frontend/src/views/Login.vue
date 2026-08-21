<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()
const username = ref('')
const password = ref('')
const loading = ref(false)

async function submit() {
  if (!username.value.trim() || !password.value) {
    ElMessage.warning('Enter your username and password')
    return
  }
  loading.value = true
  try {
    await auth.login(username.value.trim(), password.value)
    ElMessage.success('Signed in successfully')
    router.replace('/')
  } catch (error) {
    ElMessage.error(error.message || 'Sign-in failed')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="auth-page">
    <el-card class="auth-card" shadow="always">
      <div class="auth-brand"><span class="logo"><el-icon><Lock /></el-icon></span><span>GPT Auto Register</span></div>
      <h1>Sign in</h1>
      <p class="muted">Sign in to access your isolated workspace.</p>
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="Username"><el-input v-model="username" autocomplete="username" /></el-form-item>
        <el-form-item label="Password"><el-input v-model="password" type="password" show-password autocomplete="current-password" @keyup.enter="submit" /></el-form-item>
        <el-button type="primary" native-type="submit" :loading="loading" class="submit">Sign in</el-button>
      </el-form>
      <el-link type="primary" @click="router.push('/setup')">First-time setup</el-link>
    </el-card>
  </main>
</template>

<style scoped>
.auth-page { min-height: 100vh; display: grid; place-items: center; padding: 24px; background: var(--app-content-bg); }
.auth-card { width: min(420px, 100%); }
.auth-brand { display: flex; align-items: center; gap: 10px; color: var(--app-title); font-size: 18px; font-weight: 600; }
.logo { width: 32px; height: 32px; display: grid; place-items: center; border-radius: 7px; color: white; background: var(--brand); }
h1 { margin: 28px 0 4px; font-size: 28px; }
.muted { color: var(--el-text-color-secondary); margin: 0 0 22px; }
.submit { width: 100%; margin-bottom: 18px; }
</style>
