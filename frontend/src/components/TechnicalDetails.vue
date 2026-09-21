<script setup lang="ts">
/**
 * Where identifiers, versions and reason codes live.
 *
 * They are not deleted - support work needs them - but they do not belong in
 * the primary visual hierarchy, where a UUID competes with the lecture's name
 * for the operator's attention. So they collapse into one disclosure per
 * screen, copyable and verbatim.
 */
import { ref } from 'vue'
import AppIcon from './AppIcon.vue'

defineProps<{ summary?: string; entries?: [string, string][] }>()

const copied = ref('')

async function copy(value: string) {
  try {
    await navigator.clipboard.writeText(value)
    copied.value = value
    setTimeout(() => { if (copied.value === value) copied.value = '' }, 1600)
  } catch {
    copied.value = ''
  }
}
</script>

<template>
  <details class="group rounded-lg border border-line bg-[#fbfafc]">
    <summary
      class="flex cursor-pointer list-none items-center gap-2 px-3.5 py-2.5 text-xs font-semibold text-muted transition hover:text-brand-700"
    >
      <AppIcon name="chevronRight" :size="14" class="transition group-open:rotate-90" />
      {{ summary ?? 'Technical details and provenance' }}
    </summary>
    <div class="border-t border-line px-3.5 py-3">
      <dl v-if="entries?.length" class="grid gap-x-6 gap-y-2 sm:grid-cols-2">
        <div v-for="[key, value] in entries" :key="key" class="min-w-0">
          <dt class="text-2xs font-semibold uppercase tracking-kicker text-faint">{{ key }}</dt>
          <dd class="mt-0.5 flex items-start gap-1.5">
            <code class="min-w-0 break-all font-mono text-xs text-body">{{ value }}</code>
            <button
              type="button"
              class="shrink-0 rounded p-0.5 text-faint transition hover:text-brand-700"
              :aria-label="`Copy ${key}`"
              @click="copy(value)"
            >
              <AppIcon :name="copied === value ? 'check' : 'copy'" :size="13" />
            </button>
          </dd>
        </div>
      </dl>
      <slot />
    </div>
  </details>
</template>
