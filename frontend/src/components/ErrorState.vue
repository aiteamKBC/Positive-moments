<script setup lang="ts">
/**
 * What the operator sees when the API cannot answer.
 *
 * The message shown is the platform's own safe error text - a code and a short
 * sentence. Nothing here renders a stack trace, a SQL statement, a connection
 * string or a raw exception, and there is no branch that could: the component
 * only ever prints the single string it is handed.
 */
import AppIcon from './AppIcon.vue'

defineProps<{ message: string; title?: string }>()
defineEmits<{ retry: [] }>()
</script>

<template>
  <div class="surface flex flex-col items-center px-6 py-14 text-center" role="alert">
    <span class="grid h-11 w-11 place-items-center rounded-full bg-rose-50 text-rose-600">
      <AppIcon name="alert" :size="20" />
    </span>
    <h2 class="mt-4 text-base font-bold text-ink">{{ title ?? 'This could not be loaded' }}</h2>
    <p class="mt-1.5 max-w-md text-sm leading-6 text-muted">{{ message }}</p>
    <button type="button" class="btn-secondary mt-5" @click="$emit('retry')">
      <AppIcon name="refresh" :size="16" /> Try again
    </button>
  </div>
</template>
