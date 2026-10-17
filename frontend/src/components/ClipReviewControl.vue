<script setup lang="ts">
/**
 * A person's decision on one clip: pending, approved or rejected.
 *
 * Separate from the AI verdict and from "AI marked ready" by design - neither
 * is a human approval. Only a reviewer sees the buttons; everyone sees the
 * decision and who made it.
 */
import { computed, ref } from 'vue'

import StatusBadge from './StatusBadge.vue'
import { reviewClip } from '../services/api'
import type { ClipReview, ReviewStatus } from '../types'
import { cairoDateTime } from '../utils/datetime'
import { REVIEW_STATUS } from '../utils/labels'
import { errorMessage } from '../utils/format'

const props = defineProps<{
  sessionKey: string
  startCue: number
  endCue: number
  review: ClipReview
  canReview: boolean
}>()
const emit = defineEmits<{ reviewed: [review: ClipReview] }>()

const saving = ref<ReviewStatus | null>(null)
const error = ref('')
const state = computed(() => REVIEW_STATUS[props.review.status] ?? REVIEW_STATUS.pending)

async function decide(status: ReviewStatus) {
  if (saving.value) return
  saving.value = status
  error.value = ''
  try {
    emit('reviewed', await reviewClip(props.sessionKey, props.startCue, props.endCue, status))
  } catch (caught) {
    error.value = errorMessage(caught, 'The decision could not be saved. Try again.')
  } finally {
    saving.value = null
  }
}
</script>

<template>
  <div class="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-3" data-test="review">
    <span class="eyebrow">Human review</span>
    <StatusBadge :label="state.label" :tone="state.tone" data-test="review-status" />
    <span v-if="review.reviewer && review.status !== 'pending'" class="text-xs text-muted">
      {{ review.reviewer }}<template v-if="review.reviewed_at"> · {{ cairoDateTime(review.reviewed_at) }}</template>
    </span>
    <span v-if="canReview" class="ml-auto flex flex-wrap gap-2">
      <button
        v-if="review.status !== 'approved'" type="button" class="btn-secondary btn-sm" data-test="review-approve"
        :disabled="saving !== null" @click="decide('approved')"
      >{{ saving === 'approved' ? 'Saving…' : 'Approve' }}</button>
      <button
        v-if="review.status !== 'rejected'" type="button" class="btn-secondary btn-sm" data-test="review-reject"
        :disabled="saving !== null" @click="decide('rejected')"
      >{{ saving === 'rejected' ? 'Saving…' : 'Reject' }}</button>
      <button
        v-if="review.status !== 'pending'" type="button" class="btn-quiet btn-sm" data-test="review-reset"
        :disabled="saving !== null" @click="decide('pending')"
      >{{ saving === 'pending' ? 'Saving…' : 'Back to pending' }}</button>
    </span>
    <p v-if="error" class="notice-error w-full" role="alert">{{ error }}</p>
  </div>
</template>
