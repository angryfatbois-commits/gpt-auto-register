<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { getSetupStatus } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()
const username = ref('admin')
const password = ref('')
const confirm = ref('')
const loading = ref(false)
const available = ref(false)

onMounted(async () => {
  try {
    const result = await getSetupStatus()
    available.value = Boolean(result.setup_required)
    if (!available.value) router.replace('/login')
  } catch (error) {
    ElMessage.error(error.message || 'Unable to read setup status')
  }
})

async function submit() {
  if (password.value.length < 12) return ElMessage.warning('Use at least 12 characters for the administrator password')
  if (password.value !== confirm.value) return ElMessage.warning('Passwords do not match')
  loading.value = true
  try {
    await auth.setup(username.value.trim(), password.value)
    ElMessage.success('Administrator account created')
    router.replace('/')
  } catch (error) {
    ElMessage.error(error.message || 'Setup failed')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="auth-page">
    <el-card class="auth-card" shadow="always">
      <h1>Initial setup</h1>
      <p class="muted">Create the first administrator. This setup is available only once from localhost.</p>
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="Administrator username"><el-input v-model="username" autocomplete="username" /></el-form-item>
        <el-form-item label="Password"><el-input v-model="password" type="password" show-password autocomplete="new-password" /></el-form-item>
        <el-form-item label="Confirm password"><el-input v-model="confirm" type="password" show-password autocomplete="new-password" /></el-form-item>
        <el-button type="primary" native-type="submit" :loading="loading" class="submit">Create administrator</el-button>
      </el-form>
      <el-link @click="router.push('/login')">Back to sign in</el-link>
    </el-card>
  </main>
</template>

<style scoped>
.auth-page { min-height: 100vh; display: grid; place-items: center; padding: 24px; background: var(--app-content-bg); }
.auth-card { width: min(420px, 100%); }
h1 { margin-top: 0; }
.muted { color: var(--el-text-color-secondary); line-height: 1.5; }
.submit { width: 100%; margin-bottom: 18px; }
</style>
