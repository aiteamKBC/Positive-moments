<script setup lang="ts">
/**
 * "Clip" on one positive moment.
 *
 * The moment is identified by its own start_cue/end_cue - never by its place
 * in the list or its text. The request is accepted at once and the cutting
 * happens later, so success reads "Clip requested"; the finished clip shows up
 * as "View clip" when the page is next loaded.
 */
import { ref } from 'vue'

import AppIcon from './AppIcon.vue'
import { requestClip } from '../services/api'
import { errorMessage } from '../utils/format'

const props = defineProps<{ sessionKey: string, startCue: number, endCue: number }>()

const state = ref<'idle' | 'submitting' | 'requested'>('idle')
const error = ref('')
const producedUrl = ref('')

async function submit() {
  if (state.value !== 'idle') return
  state.value = 'submitting'
  error.value = ''
  try {
    await requestClip(props.sessionKey, props.startCue, props.endCue)
    state.value = 'requested'
  } catch (caught) {
    // Already produced in the meantime: show it rather than an error.
    const reply = (caught as { response?: { status?: number, data?: { clip_asset?: { url?: string } } } }).response
    if (reply?.status === 409 && reply.data?.clip_asset?.url) {
      producedUrl.value = reply.data.clip_asset.url
      state.value = 'requested'
      return
    }
    state.value = 'idle'
    error.value = errorMessage(caught, 'The clip could not be requested. Try again.')
  }
}
</script>

<template>
  <a
    v-if="producedUrl"
    class="btn-secondary btn-sm" :href="producedUrl" target="_blank" rel="noopener noreferrer"
  ><AppIcon name="external" :size="14" /> View clip</a>
  <span v-else class="inline-flex flex-wrap items-center gap-2">
    <button
      type="button"
      class="btn-secondary btn-sm"
      :disabled="state !== 'idle'"
      data-test="clip-request"
      @click="submit"
    >
      <AppIcon name="video" :size="14" />
      {{ state === 'submitting' ? 'Processing…' : state === 'requested' ? 'Clip requested · processing' : 'Clip' }}
    </button>
    <span v-if="error" class="text-xs font-semibold text-rose-700" role="alert">{{ error }}</span>
  </span>
</template>
