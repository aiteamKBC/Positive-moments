<script setup lang="ts">
/** Section tabs inside a detail page. Routing stays in the URL query. */
defineProps<{ tabs: { id: string; label: string; count?: number | null }[]; active: string }>()
defineEmits<{ select: [id: string] }>()
</script>

<template>
  <div class="no-scrollbar flex gap-1 overflow-x-auto border-b border-line" role="tablist">
    <button
      v-for="tab in tabs"
      :key="tab.id"
      type="button"
      role="tab"
      :aria-selected="tab.id === active"
      class="relative -mb-px whitespace-nowrap border-b-2 px-3.5 py-2.5 text-sm font-semibold transition"
      :class="tab.id === active
        ? 'border-brand-700 text-brand-700'
        : 'border-transparent text-muted hover:border-line hover:text-ink'"
      @click="$emit('select', tab.id)"
    >
      {{ tab.label }}
      <span
        v-if="tab.count !== undefined && tab.count !== null"
        class="ml-1.5 rounded px-1.5 py-0.5 text-2xs tabular-nums"
        :class="tab.id === active ? 'bg-brand-50 text-brand-700' : 'bg-slate-100 text-muted'"
      >{{ tab.count }}</span>
    </button>
  </div>
</template>
