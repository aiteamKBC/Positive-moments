<script setup lang="ts">
/**
 * A compact filter row, with the rest behind a disclosure.
 *
 * The filters this product needs do not fit on one line, and the previous
 * design solved that by growing a four-row panel that pushed the table - the
 * actual product - below the fold. So the primary slot holds the filters
 * people use on most visits, and `more` holds the rest.
 *
 * Nothing essential is hidden: the count of active hidden filters is shown on
 * the toggle, and the panel opens automatically when any of them is set.
 */
import { ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'

const props = withDefaults(defineProps<{ activeCount?: number; hiddenActive?: number }>(), {
  activeCount: 0,
  hiddenActive: 0,
})
defineEmits<{ reset: [] }>()

const open = ref(props.hiddenActive > 0)
watch(() => props.hiddenActive, (value) => { if (value > 0) open.value = true })
</script>

<template>
  <section aria-label="Filters">
    <div class="filter-bar">
      <slot />
      <div class="ml-auto flex items-center gap-2">
        <button
          v-if="$slots.more"
          type="button"
          class="btn btn-sm border border-line bg-white text-muted hover:border-brand-300 hover:text-brand-700"
          :aria-expanded="open"
          @click="open = !open"
        >
          <AppIcon name="filter" :size="14" />
          More filters
          <span v-if="hiddenActive" class="rounded bg-brand-50 px-1.5 text-2xs font-bold text-brand-700">{{ hiddenActive }}</span>
        </button>
        <button
          type="button"
          class="btn btn-sm px-2 text-muted hover:text-brand-700 disabled:opacity-40"
          :disabled="!activeCount"
          @click="$emit('reset')"
        >
          Reset
        </button>
      </div>
    </div>
    <div v-if="open && $slots.more" class="surface mt-2 flex flex-wrap items-center gap-2 px-3 py-2.5">
      <slot name="more" />
    </div>
  </section>
</template>
