<script setup lang="ts">
withDefaults(defineProps<{ rows?: number }>(), { rows: 4 })
</script>

<template>
  <div class="animate-pulse space-y-4" aria-label="Loading content" aria-live="polite">
    <div v-for="row in rows" :key="row" class="surface h-24 bg-white p-5">
      <div class="h-3 w-1/3 rounded bg-slate-200" />
      <div class="mt-4 h-3 w-2/3 rounded bg-slate-100" />
    </div>
  </div>
</template>

