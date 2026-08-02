<script setup lang="ts">
import { computed } from 'vue'
import { formatStatus } from '../utils/format'

const props = defineProps<{ value?: string | null }>()
const tone = computed(() => {
  const value = props.value?.toLowerCase() ?? ''
  if (['completed', 'complete', 'success', 'ready', 'accept'].some((item) => value.includes(item))) {
    return 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-600/10'
  }
  if (['error', 'failed', 'reject'].some((item) => value.includes(item))) {
    return 'bg-rose-50 text-rose-700 ring-1 ring-rose-600/10'
  }
  return 'bg-amber-50 text-amber-700 ring-1 ring-amber-600/10'
})
</script>

<template>
  <span class="badge" :class="tone">{{ formatStatus(value) }}</span>
</template>

