<script setup lang="ts">
/**
 * The single icon system.
 *
 * One 24px grid, one 1.75 stroke, round caps and joins throughout. Every icon
 * in the product comes from this file, so nothing can quietly arrive from a
 * different family with a different weight - which is exactly how an interface
 * starts looking assembled rather than designed.
 *
 * Icons clarify; they do not decorate. Anything purely ornamental is left out.
 */
const PATHS: Record<string, string> = {
  operations: 'M3 13a9 9 0 0 1 18 0M12 13l4-3M3 20h18',
  lectures: 'M4 5.5A2.5 2.5 0 0 1 6.5 3H19v15H6.5A2.5 2.5 0 0 0 4 20.5zM4 20.5A2.5 2.5 0 0 1 6.5 18H19v3H6.5A2.5 2.5 0 0 1 4 20.5M8.5 7.5h6M8.5 11h4',
  moments: 'M12 20.5 4.8 13a4.6 4.6 0 0 1 6.5-6.5l.7.7.7-.7A4.6 4.6 0 0 1 19.2 13ZM17 3v3M20 4.5h-3',
  calendar: 'M4 8.5h16M7.5 3.5v3M16.5 3.5v3M5 5.5h14a1 1 0 0 1 1 1V19a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6.5a1 1 0 0 1 1-1Z',
  search: 'M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14ZM20 20l-4-4',
  filter: 'M4 6h16M7 12h10M10 18h4',
  refresh: 'M20 12a8 8 0 1 1-2.6-5.9M20 4v4h-4',
  alert: 'M12 8.5v4.5M12 16.5h.01M10.6 3.9 2.7 17.5A1.6 1.6 0 0 0 4.1 20h15.8a1.6 1.6 0 0 0 1.4-2.5L13.4 3.9a1.6 1.6 0 0 0-2.8 0Z',
  check: 'm5 12.5 4.5 4.5L19 7',
  checkCircle: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm-3.5-9.2 2.6 2.6 4.6-5',
  clock: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM12 7.5V12l3 2',
  users: 'M15.5 20v-1.5a3.5 3.5 0 0 0-3.5-3.5H7a3.5 3.5 0 0 0-3.5 3.5V20M9.5 11.5a3.25 3.25 0 1 0 0-6.5 3.25 3.25 0 0 0 0 6.5ZM20.5 20v-1.5a3.5 3.5 0 0 0-2.6-3.4M15.5 5.2a3.25 3.25 0 0 1 0 6.1',
  video: 'M15 9.5 20.5 6v12L15 14.5M4.5 5.5h9a1.5 1.5 0 0 1 1.5 1.5v10a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 3 17V7a1.5 1.5 0 0 1 1.5-1.5Z',
  play: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM10 8.5l5.5 3.5L10 15.5Z',
  external: 'M14 4h6v6M20 4l-8.5 8.5M18 14.5V19a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h4.5',
  copy: 'M9 9.5A1.5 1.5 0 0 1 10.5 8h8A1.5 1.5 0 0 1 20 9.5v8a1.5 1.5 0 0 1-1.5 1.5h-8A1.5 1.5 0 0 1 9 17.5ZM15 8V6.5A1.5 1.5 0 0 0 13.5 5h-8A1.5 1.5 0 0 0 4 6.5v8A1.5 1.5 0 0 0 5.5 16H7',
  chevronLeft: 'm14.5 5.5-6 6.5 6 6.5',
  chevronRight: 'm9.5 5.5 6 6.5-6 6.5',
  chevronDown: 'm5.5 9.5 6.5 6 6.5-6',
  arrowRight: 'M4 12h15m-5.5-5.5L19 12l-5.5 5.5',
  menu: 'M4 7h16M4 12h16M4 17h16',
  close: 'm6 6 12 12M18 6 6 18',
  info: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM12 11v5.5M12 7.8h.01',
  signOut: 'M15 8V6.5A1.5 1.5 0 0 0 13.5 5h-7A1.5 1.5 0 0 0 5 6.5v11A1.5 1.5 0 0 0 6.5 19h7a1.5 1.5 0 0 0 1.5-1.5V16M11 12h9m-3-3 3 3-3 3',
  quote: 'M9 6.5C6.5 7.8 5 10 5 12.8V17h5v-5H7.6c.2-1.7 1-2.9 2.4-3.7ZM19 6.5c-2.5 1.3-4 3.5-4 6.3V17h5v-5h-2.4c.2-1.7 1-2.9 2.4-3.7Z',
  sparkle: 'm12 4 1.9 4.9L19 10.8l-5.1 1.9L12 17.6l-1.9-4.9L5 10.8l5.1-1.9ZM18.5 16.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7Z',
  layers: 'm12 3 8.5 4.5L12 12 3.5 7.5ZM3.5 12 12 16.5 20.5 12M3.5 16.5 12 21l8.5-4.5',
  slash: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM5.6 5.6l12.8 12.8',
  inbox: 'M4 13h4l1.5 2.5h5L16 13h4M4.8 6.4 3.2 12.6a2 2 0 0 0-.2.8V18a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1v-4.6a2 2 0 0 0-.2-.8l-1.6-6.2A1.5 1.5 0 0 0 17.7 5H6.3a1.5 1.5 0 0 0-1.5 1.4Z',
}

withDefaults(defineProps<{ name: keyof typeof PATHS | string; size?: number }>(), { size: 18 })
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.75"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    focusable="false"
    class="shrink-0"
  >
    <path :d="PATHS[name] ?? PATHS.info" />
  </svg>
</template>
