<script setup lang="ts">
/**
 * One page header pattern for every screen.
 *
 * The kicker names the AREA, not the product - repeating "Lecture
 * Intelligence" above every title says nothing the header bar has not already
 * said, and hierarchy that repeats itself stops being hierarchy.
 */
defineProps<{ kicker?: string; title: string; lede?: string }>()
</script>

<template>
  <header class="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
    <div class="min-w-0">
      <p v-if="kicker" class="kicker">{{ kicker }}</p>
      <h1 class="page-title" :class="kicker ? 'mt-1.5' : ''">{{ title }}</h1>
      <p v-if="lede" class="page-lede">{{ lede }}</p>
      <slot name="meta" />
    </div>
    <div v-if="$slots.actions" class="flex shrink-0 flex-wrap items-center gap-2">
      <slot name="actions" />
    </div>
  </header>
</template>
