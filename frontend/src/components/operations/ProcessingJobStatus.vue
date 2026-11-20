<script setup lang="ts">
/**
 * One background processing job, as the server reports it: Queued, Running,
 * Completed or Failed, with its attempts and the reason it last stopped. The
 * worker owns every transition; this only formats them.
 */
import { computed } from 'vue'

import StatusBadge from '../StatusBadge.vue'
import type { ProcessingJob } from '../../types/operations'
import { cairoDateTime } from '../../utils/datetime'
import { actionLabel, humanise } from '../../utils/labels'

const props = defineProps<{ job: ProcessingJob; compact?: boolean }>()

const LABELS = {
  QUEUED: { label: 'Queued', tone: 'waiting' },
  RUNNING: { label: 'Running', tone: 'info' },
  COMPLETED: { label: 'Completed', tone: 'ok' },
  FAILED: { label: 'Failed', tone: 'error' },
} as const

const badge = computed(() => LABELS[props.job.status] ?? { label: humanise(props.job.status), tone: 'quiet' })
/** Which request this job is: a manual upload's processing, or a Teams transcript retry. */
const teamsRetry = computed(() => props.job.reason === 'TEAMS_TRANSCRIPT_RETRY')
const kind = computed(() => (teamsRetry.value ? 'Teams transcript retry' : 'Uploaded transcript processing'))
const retrying = computed(() => props.job.status === 'QUEUED' && props.job.attempt_count > 0)

const detail = computed(() => {
  const job = props.job
  switch (job.status) {
    case 'QUEUED':
      return retrying.value
        ? `Attempt ${job.attempt_count} of ${job.max_attempts} stopped (${humanise(job.last_error_code)}); `
          + `next try after ${cairoDateTime(job.next_attempt_after)}.`
        : `Waiting for the background worker since ${cairoDateTime(job.created_at)}.`
    case 'RUNNING':
      return `${teamsRetry.value ? 'Asking Microsoft Teams for the transcript, then processing' : 'Processing'}`
        + ` since ${cairoDateTime(job.started_at)} (attempt ${job.attempt_count} of ${job.max_attempts}). `
        + 'The quality evaluation can take several minutes.'
    case 'COMPLETED':
      return `Finished ${cairoDateTime(job.finished_at)}.`
        + (teamsRetry.value && job.outcome?.teams ? ' The transcript was retrieved from Teams.' : '')
        + (job.outcome?.final_action ? ` Next for this lecture: ${actionLabel(job.outcome.final_action)}.` : '')
    case 'FAILED':
      return `Stopped ${cairoDateTime(job.finished_at)} after ${job.attempt_count} attempt`
        + `${job.attempt_count === 1 ? '' : 's'}: `
        + `${(job.last_error_message || humanise(job.last_error_code)).replace(/\.\s*$/, '')}.`
    default:
      return ''
  }
})
</script>

<template>
  <div :class="compact ? '' : 'flex items-start gap-2'" data-test="processing-job">
    <StatusBadge :label="retrying ? 'Retry scheduled' : badge.label" :tone="badge.tone" :title="job.status" />
    <p class="text-xs leading-5 text-muted" :class="compact ? 'mt-1' : ''">
      <span class="font-semibold text-body" data-test="processing-job-kind">{{ kind }}:</span> {{ detail }}
    </p>
  </div>
</template>
