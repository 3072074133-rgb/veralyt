<script setup lang="ts">
import AppSidebar from './components/AppSidebar.vue'
import { ref, watch } from 'vue'
const collapsed = ref(localStorage.getItem('analyse-agent.sidebar-collapsed') === 'true')
watch(collapsed, (value) => localStorage.setItem('analyse-agent.sidebar-collapsed', String(value)))
</script>

<template>
  <div class="app-shell" :class="{ 'sidebar-collapsed': collapsed }">
    <AppSidebar :collapsed="collapsed" @toggle="collapsed = !collapsed" />
    <router-view />
  </div>
</template>
