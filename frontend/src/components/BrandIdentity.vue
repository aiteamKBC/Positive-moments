<script setup lang="ts">
/**
 * The product lockup: the official KBC crest beside the product name.
 *
 * The crest is the college's own asset, stored locally (see
 * public/assets/brand/README.md). It is never redrawn and never fetched from
 * the public website at runtime.
 *
 * The organisation is the small line and the product is the large one, because
 * everyone using this already knows where they work; what they need to know is
 * which internal tool they are looking at.
 */
withDefaults(defineProps<{ size?: 'sm' | 'md' | 'lg'; inverse?: boolean }>(), {
  size: 'md',
  inverse: false,
})

const MARK = { sm: 'h-8 w-8', md: 'h-9 w-9', lg: 'h-12 w-12' }
const NAME = { sm: 'text-[0.9rem]', md: 'text-[1.02rem]', lg: 'text-xl' }
</script>

<template>
  <span class="flex min-w-0 items-center gap-2.5">
    <img
      src="/assets/brand/kbc-favicon.svg"
      alt="Kent Business College"
      class="shrink-0 rounded-md bg-white object-contain p-0.5 ring-1 ring-black/5"
      :class="MARK[size]"
    />
    <span class="min-w-0 leading-none">
      <span
        class="block truncate text-[0.62rem] font-bold uppercase tracking-[0.15em]"
        :class="inverse ? 'text-white/65' : 'text-brand-600'"
      >Kent Business College</span>
      <span
        class="mt-1 block truncate font-semibold tracking-tight"
        :class="[NAME[size], inverse ? 'text-white' : 'text-ink']"
      >Lecture Intelligence</span>
    </span>
  </span>
</template>
