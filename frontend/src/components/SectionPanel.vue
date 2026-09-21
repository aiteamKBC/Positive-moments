<script setup lang="ts">
/**
 * A titled surface. Used for every section that holds a table or a list, so
 * that section chrome is defined once rather than re-invented per page.
 */
defineProps<{ title: string; count?: number | string | null; note?: string; flush?: boolean }>()
</script>

<template>
  <section class="panel">
    <div class="panel-head">
      <div class="min-w-0">
        <h2 class="section-title">
          {{ title }}
          <span v-if="count !== undefined && count !== null" class="ml-1.5 font-normal text-faint tabular-nums">{{ count }}</span>
        </h2>
        <p v-if="note" class="mt-0.5 text-xs text-muted">{{ note }}</p>
      </div>
      <div v-if="$slots.actions" class="flex shrink-0 items-center gap-2"><slot name="actions" /></div>
    </div>
    <div :class="flush ? '' : 'panel-body'"><slot /></div>
  </section>
</template>
