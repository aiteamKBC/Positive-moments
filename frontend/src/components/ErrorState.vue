<script setup lang="ts">
defineProps<{ message: string }>()
defineEmits<{ retry: [] }>()
</script>

<template>
  <div class="surface flex flex-col items-center px-6 py-14 text-center" role="alert">
    <span class="grid h-12 w-12 place-items-center rounded-full bg-rose-50 text-rose-600">
      <svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="12" cy="12" r="9"/><path d="M12 7v6m0 4h.01"/>
      </svg>
    </span>
    <h2 class="mt-4 text-lg font-bold">We couldn’t load this data</h2>
    <p class="mt-1 max-w-md text-sm text-muted">{{ message }}</p>
    <button type="button" class="btn-primary mt-5" @click="$emit('retry')">Retry</button>
  </div>
</template>

