<script setup lang="ts">
/**
 * Empty is not error, and empty is not failure.
 *
 * Most empty states in an operations console are GOOD news - no failures, no
 * reviews, nothing waiting - so the default tone is a calm tick rather than a
 * warning. A state that genuinely needs an explanation passes its own icon and
 * an action.
 */
import AppIcon from './AppIcon.vue'

withDefaults(defineProps<{
  title: string
  message?: string
  icon?: string
  tone?: 'good' | 'neutral'
  compact?: boolean
}>(), { message: undefined, icon: 'check', tone: 'good', compact: false })
</script>

<template>
  <div class="flex flex-col items-center px-6 text-center" :class="compact ? 'py-8' : 'py-14'">
    <span
      class="grid h-10 w-10 place-items-center rounded-full"
      :class="tone === 'good' ? 'bg-emerald-50 text-emerald-600' : 'bg-slate-100 text-muted'"
    >
      <AppIcon :name="icon" :size="18" />
    </span>
    <p class="mt-3 text-sm font-semibold text-ink">{{ title }}</p>
    <p v-if="message" class="mx-auto mt-1 max-w-md text-sm leading-6 text-muted">{{ message }}</p>
    <div v-if="$slots.action" class="mt-5 flex flex-wrap justify-center gap-2"><slot name="action" /></div>
  </div>
</template>
