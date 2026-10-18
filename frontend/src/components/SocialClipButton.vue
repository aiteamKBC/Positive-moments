<script setup lang="ts">
/**
 * Create Clip on one Social Media reel - the Positive Moments clip button's
 * behaviour, for an opportunity instead of a moment.
 *
 * The request goes to Django, which forwards it to the same clip-on-demand
 * webhook. Acceptance reads "Clip requested · processing"; the clip is shown as
 * ready only when the API reports the SharePoint link n8n saved. A refusal
 * that carries the current state (already requested, already produced) shows
 * that state rather than an error.
 *
 * When the moment already has a padded Positive Moments / OFSTED clip, the
 * wording says "exact reel", so the two videos are never confused.
 */
import { ref } from 'vue'

import AppIcon from './AppIcon.vue'
import { requestSocialClip } from '../services/social'
import type { SocialClip } from '../types/social'
import { errorMessage } from '../utils/format'

const props = withDefaults(defineProps<{
  sessionKey: string
  opportunityId: number
  clip: SocialClip
  hasExistingClip?: boolean
}>(), { hasExistingClip: false })
const emit = defineEmits<{ update: [clip: SocialClip] }>()

const submitting = ref(false)
const error = ref('')

async function submit() {
  if (submitting.value || !props.clip.can_request) return
  submitting.value = true
  error.value = ''
  try {
    emit('update', await requestSocialClip(props.sessionKey, props.opportunityId))
  } catch (caught) {
    const reply = (caught as { response?: { data?: { clip?: SocialClip } } }).response
    if (reply?.data?.clip) {
      emit('update', reply.data.clip)
      return
    }
    error.value = errorMessage(caught, 'The clip could not be requested. Try again.')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <a
    v-if="clip.state === 'ready' && clip.clip_url"
    class="btn-secondary btn-sm" :href="clip.clip_url" target="_blank" rel="noopener noreferrer"
    :title="clip.clip_filename ?? undefined" data-test="view-clip"
  ><AppIcon name="external" :size="14" /> {{ hasExistingClip ? 'View Exact Reel' : 'View Clip' }}</a>

  <span v-else-if="clip.state === 'requested'" class="badge-waiting" data-test="clip-requested">
    {{ hasExistingClip ? 'Exact reel requested · processing' : 'Clip requested · processing' }}
  </span>

  <span v-else-if="clip.can_request" class="inline-flex flex-wrap items-center gap-2" data-test="clip-action">
    <button
      type="button" class="btn-secondary btn-sm" :disabled="submitting" data-test="create-clip" @click="submit"
    >
      <AppIcon name="video" :size="14" />
      {{ submitting ? 'Requesting…' : clip.state === 'failed' || clip.state === 'stale' ? 'Try again'
        : hasExistingClip ? 'Create Exact Reel' : 'Create Clip' }}
    </button>
    <span v-if="clip.state === 'failed'" class="text-xs font-semibold text-rose-700" data-test="clip-failed">
      Clip failed{{ clip.error ? `: ${clip.error}` : '' }}
    </span>
    <span v-else-if="clip.state === 'stale'" class="text-xs text-muted" data-test="clip-stale">{{ clip.reason }}</span>
    <span v-if="error" class="text-xs font-semibold text-rose-700" role="alert" data-test="clip-error">{{ error }}</span>
  </span>

  <span
    v-else class="badge-quiet" :title="clip.reason ?? undefined" data-test="clip-unavailable"
  >Clip unavailable</span>
</template>
