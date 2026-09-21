<script setup lang="ts">
/**
 * Loading states that hold the page's real shape.
 *
 * Deliberately NOT a spinner over a zeroed dashboard: flashing "0 waiting"
 * while the day is still loading states something false for as long as the
 * request takes, and the day report is a slow read.
 */
withDefaults(defineProps<{ variant?: 'table' | 'tiles' | 'cards'; rows?: number }>(), {
  variant: 'table',
  rows: 6,
})
</script>

<template>
  <div aria-busy="true" aria-live="polite">
    <span class="sr-only">Loading</span>

    <div v-if="variant === 'tiles'" class="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
      <div class="skeleton h-[5.5rem] lg:col-span-1" />
      <div v-for="index in 4" :key="index" class="skeleton h-[5.5rem]" />
    </div>

    <div v-else-if="variant === 'cards'" class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <div v-for="index in rows" :key="index" class="skeleton h-28" />
    </div>

    <div v-else class="surface overflow-hidden">
      <div class="border-b border-line bg-[#fbfafc] px-4 py-3"><div class="skeleton h-3 w-40" /></div>
      <div v-for="index in rows" :key="index" class="flex items-center gap-4 border-b border-line/70 px-4 py-3.5 last:border-0">
        <div class="skeleton h-3 flex-1" :style="{ maxWidth: `${28 + ((index * 13) % 24)}%` }" />
        <div class="skeleton hidden h-3 w-24 sm:block" />
        <div class="skeleton hidden h-3 w-20 md:block" />
        <div class="skeleton h-5 w-16 rounded-full" />
      </div>
    </div>
  </div>
</template>
