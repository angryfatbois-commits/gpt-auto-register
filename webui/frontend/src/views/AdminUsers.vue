<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const users = ref([])
const loading = ref(false)
const form = ref({ username: '', password: '', role: 'user' })

async function refresh() {
  loading.value = true
  try { users.value = (await auth.listUsers()).items || [] } catch (error) { ElMessage.error(error.message || 'Unable to load users') } finally { loading.value = false }
}

async function create() {
  if (!form.value.username.trim() || form.value.password.length < 12) {
    ElMessage.warning('Enter a username and a password of at least 12 characters')
    return
  }
  try {
    await auth.createUser({ ...form.value, username: form.value.username.trim() })
    form.value = { username: '', password: '', role: 'user' }
    ElMessage.success('User created')
    await refresh()
  } catch (error) { ElMessage.error(error.message || 'Unable to create user') }
}

async function disable(user) {
  try {
    await ElMessageBox.confirm(`Disable ${user.username}? Their session will be revoked.`, 'Confirm action', { type: 'warning' })
    await auth.disableUser(user.id)
    ElMessage.success('User disabled')
    await refresh()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || 'Unable to disable user')
  }
}

async function resetPassword(user) {
  try {
    const password = await ElMessageBox.prompt('Enter a new password (at least 12 characters)', 'Reset password', { inputType: 'password', inputPattern: /.{12,}/, inputErrorMessage: 'Password must be at least 12 characters' })
    await auth.resetUserPassword(user.id, password.value)
    ElMessage.success('Password reset; existing sessions were revoked')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || 'Unable to reset password')
  }
}

onMounted(refresh)
</script>

<template>
  <section>
    <div class="page-head"><div><h2>User management</h2><p>Create isolated workspaces and manage access.</p></div><el-button :loading="loading" @click="refresh">Refresh</el-button></div>
    <el-card class="create-card" shadow="never">
      <template #header><span>Create user</span></template>
      <el-form inline @submit.prevent="create">
        <el-form-item label="Username"><el-input v-model="form.username" autocomplete="off" /></el-form-item>
        <el-form-item label="Password"><el-input v-model="form.password" type="password" show-password autocomplete="new-password" /></el-form-item>
        <el-form-item label="Role"><el-select v-model="form.role"><el-option label="User" value="user" /><el-option label="Administrator" value="admin" /></el-select></el-form-item>
        <el-form-item><el-button type="primary" @click="create">Create user</el-button></el-form-item>
      </el-form>
    </el-card>
    <el-card shadow="never">
      <el-table v-loading="loading" :data="users" stripe>
        <el-table-column prop="username" label="Username" />
        <el-table-column prop="role" label="Role" />
        <el-table-column label="Status"><template #default="scope"><el-tag :type="scope.row.active ? 'success' : 'info'">{{ scope.row.active ? 'Active' : 'Disabled' }}</el-tag></template></el-table-column>
        <el-table-column label="Actions" width="220"><template #default="scope"><el-button size="small" @click="resetPassword(scope.row)">Reset password</el-button><el-button v-if="scope.row.active && scope.row.id !== auth.user?.id" size="small" type="danger" plain @click="disable(scope.row)">Disable</el-button></template></el-table-column>
      </el-table>
    </el-card>
  </section>
</template>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
h2 { margin: 0 0 4px; }
.page-head p { margin: 0; color: var(--el-text-color-secondary); }
.create-card { margin-bottom: 16px; }
</style>
