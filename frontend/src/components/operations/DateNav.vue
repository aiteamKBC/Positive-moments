<script setup lang="ts">
/**
 * Day navigation: previous, next, today, and a picker.
 *
 * The dates come from the server, not from the browser's clock. A lecture's
 * business date is its Africa/Cairo date, so a console that computed
 * "yesterday" locally would show the wrong day to anyone outside that
 * timezone - and for three hours every evening, to everyone.
 */
import AppIcon from '../AppIcon.vue'
import { businessDateLong } from '../../utils/datetime'
import type { Navigation } from '../../types/operations'

defineProps<{ navigation: Navigation; loading?: boolean }>()
const emit = defineEmits<{ (event: 'navigate', date: string): void }>()

function pick(event: Event) {
  const value = (event.target as HTMLInputElement).value
  if (value) emit('navigate', value)
}
</script>

<template>
  <div class="surface flex flex-wrap items-center gap-2 px-3 py-2">
    <div class="flex items-center gap-1">
      <button
        type="button" class="icon-btn" :disabled="loading" aria-label="Previous day"
        @click="emit('navigate', navigation.previous_date)"
      >
        <AppIcon name="chevronLeft" :size="16" />
      </button>
      <button
        type="button" class="icon-btn" :disabled="loading" aria-label="Next day"
        @click="emit('navigate', navigation.next_date)"
      >
        <AppIcon name="chevronRight" :size="16" />
      </button>
    </div>

    <p class="min-w-0 px-1 text-sm font-semibold text-ink">
      {{ businessDateLong(navigation.date) }}
      <span v-if="navigation.is_today" class="ml-1.5 chip">Today</span>
    </p>

    <div class="ml-auto flex flex-wrap items-center gap-2">
      <span
        v-if="navigation.is_future"
        class="badge-quiet"
      >Future date — nothing discovered yet</span>
      <label class="sr-only" for="day-picker">Choose a business day</label>
      <input
        id="day-picker"
        type="date"
        class="field-sm w-auto"
        :value="navigation.date"
        :disabled="loading"
        @change="pick"
      />
      <button
        type="button"
        class="btn btn-sm border border-line bg-white text-ink hover:border-brand-300 hover:bg-brand-50"
        :disabled="loading || navigation.is_today"
        @click="emit('navigate', navigation.today)"
      >
        Today
      </button>
      <span class="hidden text-2xs text-faint lg:inline">{{ navigation.timezone }}</span>
    </div>
  </div>
</template>
