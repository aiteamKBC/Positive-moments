<script setup lang="ts">
/**
 * A metric, sized by how much it matters.
 *
 * `lead` is the one figure that answers the page's question; everything else
 * is a compact supporting tile. Giving every number the same visual weight is
 * the fastest way to make a dashboard unreadable, so the variant is a required
 * decision at the call site rather than a default.
 *
 * A zero is only ever shown when it is a real, known zero. `unknown` renders
 * an em dash instead, because "0 waiting" and "we have not been told" are
 * different facts.
 */
import { computed } from 'vue'
import AppIcon from './AppIcon.vue'

const props = withDefaults(defineProps<{
  label: string
  value?: number | string | null
  note?: string
  lead?: boolean
  tone?: 'neutral' | 'ok' | 'waiting' | 'review' | 'error'
  icon?: string
  unknown?: boolean
  loading?: boolean
}>(), {
  value: undefined, note: undefined, icon: undefined,
  lead: false, tone: 'neutral', unknown: false, loading: false,
})

const VALUE_TONE = {
  neutral: 'text-ink',
  ok: 'text-emerald-700',
  waiting: 'text-amber-700',
  review: 'text-orange-700',
  error: 'text-rose-700',
}

/** A count of zero is quieted so the eye goes to the columns that need work. */
const muted = computed(() => !props.lead && props.value === 0)
</script>

<template>
  <div :class="lead ? 'tile-lead' : 'tile'">
    <div class="flex items-center gap-1.5">
      <AppIcon v-if="icon" :name="icon" :size="14" :class="lead ? 'text-white/70' : 'text-faint'" />
      <p class="tile-label" :class="lead ? '!text-white/70' : ''">{{ label }}</p>
    </div>
    <p v-if="loading" class="skeleton mt-2 h-7 w-12" />
    <p
      v-else
      class="tile-value"
      :class="[lead ? '!text-white !text-4xl' : VALUE_TONE[tone], muted ? '!text-faint' : '']"
    >
      {{ unknown ? '—' : value }}
    </p>
    <p v-if="note" class="tile-note" :class="lead ? '!text-white/70' : ''">{{ note }}</p>
    <slot />
  </div>
</template>
