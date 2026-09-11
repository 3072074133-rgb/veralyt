<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { ClientSideRowModelModule, ModuleRegistry, PaginationModule } from 'ag-grid-community'
import { AgGridVue } from 'ag-grid-vue3'
import 'ag-grid-community/styles/ag-grid.css'
import 'ag-grid-community/styles/ag-theme-quartz.css'
import { X } from 'lucide-vue-next'
import type { EvidenceRecord } from '../types'

ModuleRegistry.registerModules([ClientSideRowModelModule, PaginationModule])

const props = defineProps<{ open: boolean; evidence?: EvidenceRecord }>()
const emit = defineEmits<{ close: [] }>()
const drawer = ref<HTMLElement>()
let previousFocus: HTMLElement | null = null
const columnDefs = computed(() => (props.evidence?.columns ?? []).map((field) => ({ field, sortable: true, filter: true, resizable: true, minWidth: 130 })))
watch(() => props.open, async (open) => {
  if (open) { previousFocus = document.activeElement as HTMLElement; await nextTick(); drawer.value?.focus() }
  else previousFocus?.focus()
})
function onKeydown(event: KeyboardEvent) { if (event.key === 'Escape') emit('close') }
</script>

<template>
  <div v-if="open" class="drawer-backdrop" @click.self="$emit('close')">
    <section ref="drawer" class="evidence-drawer" role="dialog" aria-modal="true" aria-labelledby="evidence-title" tabindex="-1" @keydown="onKeydown">
      <header><div><h2 id="evidence-title">{{ evidence?.title ?? '证据明细' }}</h2><p>{{ evidence?.rows.length ?? 0 }} 行 · {{ evidence?.source }}</p></div><button class="icon-button" title="关闭" @click="emit('close')"><X :size="19" /></button></header>
      <AgGridVue class="ag-theme-quartz evidence-grid" :column-defs="columnDefs" :row-data="evidence?.rows ?? []" :pagination="true" :pagination-page-size="20" />
    </section>
  </div>
</template>
