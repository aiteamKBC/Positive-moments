<script setup lang="ts">
/**
 * Edit AI Prompts: the two System Messages the Social Media workflows use.
 *
 * Each prompt is edited and saved on its own. The text is read from and saved
 * to PostgreSQL through Django; n8n reads the newest version of each before
 * every analysis. Closing with unsaved edits asks first.
 */
import { onBeforeUnmount, onMounted, reactive } from 'vue'

import AppIcon from './AppIcon.vue'
import SocialPromptEditor from './SocialPromptEditor.vue'
import type { PromptKey } from '../types/social'

const emit = defineEmits<{ close: [] }>()

const PROMPTS: { key: PromptKey; title: string; description: string }[] = [
  {
    key: 'social_discovery', title: 'Social Discovery',
    description: 'Finds candidate social media opportunities in the lecture transcript.',
  },
  {
    key: 'social_verification', title: 'Social Verification',
    description: 'Checks each candidate before it is saved as an opportunity.',
  },
]

const dirty = reactive<Record<string, boolean>>({})

function close() {
  if (Object.values(dirty).some(Boolean)
    && !window.confirm('You have unsaved prompt changes. Close and discard them?')) return
  emit('close')
}

function onKey(event: { key: string }) {
  if (event.key === 'Escape') close()
}
onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div
    class="fixed inset-0 z-50 grid place-items-center bg-brand-950/60 p-2 backdrop-blur-sm sm:p-4"
    role="presentation" @click.self="close"
  >
    <section
      class="surface flex max-h-[94vh] w-full max-w-[1400px] flex-col overflow-hidden"
      role="dialog" aria-modal="true" aria-labelledby="prompts-title" data-test="prompts-dialog"
    >
      <div class="panel-head">
        <div>
          <p class="kicker">Social Media</p>
          <h2 id="prompts-title" class="mt-1 text-lg font-bold text-ink">Edit AI Prompts</h2>
        </div>
        <button type="button" class="icon-btn" aria-label="Close" data-test="close-prompts" @click="close">
          <AppIcon name="close" :size="16" />
        </button>
      </div>

      <div class="overflow-y-auto bg-canvas px-3 py-4 sm:px-5">
        <p class="notice mb-4" role="note">
          <AppIcon name="info" :size="16" class="mt-0.5" />
          <span>
            These are the exact System Messages the Social Media workflows send to the AI. A saved change is used from
            the next analysis; earlier versions are kept and can be restored.
          </span>
        </p>
        <div class="grid gap-4 xl:grid-cols-2">
          <SocialPromptEditor
            v-for="prompt in PROMPTS" :key="prompt.key"
            :prompt-key="prompt.key" :title="prompt.title" :description="prompt.description"
            @dirty="dirty[prompt.key] = $event"
          />
        </div>
      </div>
    </section>
  </div>
</template>
