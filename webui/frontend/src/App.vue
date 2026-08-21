<script setup>
import en from 'element-plus/es/locale/lang/en'
</script>

<template>
  <el-config-provider :locale="en">
    <router-view v-slot="{ Component, route }">
      <component :is="route.meta?.public ? Component : 'AdminLayout'">
        <component v-if="!route.meta?.public" :is="Component" />
      </component>
    </router-view>
  </el-config-provider>
</template>

<script>
import AdminLayout from '@/layouts/AdminLayout.vue'

export default { components: { AdminLayout } }
</script>
