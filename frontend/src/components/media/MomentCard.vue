<script setup lang="ts">
/**
 * One Positive Moment: the evidence, where its clip comes from, and what has
 * happened to that clip. The actions shown depend only on the state the
 * platform reports - a delivered clip offers "Open", never "Render".
 */
import { computed, ref } from 'vue'

import AppIcon from '../AppIcon.vue'
import StatusBadge from '../StatusBadge.vue'
import type { PositiveMoment } from '../../types/media'
import { cairoDateTime } from '../../utils/datetime'
import { formatConfidence } from '../../utils/format'
import { MOMENT_CATEGORIES, clock, humanise, mediaStatus } from '../../utils/labels'

const props = defineProps<{ moment: PositiveMoment; busy?: boolean }>()
const emit = defineEmits<{
  (e: 'render', momentId: string): void
  (e: 'retry', momentId: string): void
  (e: 'open-clip', assetId: string): void
  (e: 'open-recording'): void
}>()

const showDialogue = ref(false)
const showTimeline = ref(false)
const job = computed(() => props.moment.job)
const status = computed(() => mediaStatus(props.moment.asset ? 'COMPLETED' : job.value?.status))
const canRender = computed(() => !props.moment.asset && job.value?.status === 'READY_TO_RENDER')
const canRetry = computed(() => !props.moment.asset
  && (job.value?.status === 'FAILED_RETRYABLE' || job.value?.status === 'FAILED_FINAL'))
const evidence = computed(() => `${clock(props.moment.evidence_start_ms / 1000)} → ${clock(props.moment.evidence_end_ms / 1000)}`)
const requested = computed(() => job.value?.requested_media_start_seconds === null || !job.value
  ? '—'
  : `${clock(job.value.requested_media_start_seconds)} → ${clock(job.value.requested_media_end_seconds)}`)
const actual = computed(() => job.value?.actual_media_start_seconds === null || !job.value
  ? '—'
  : `${clock(job.value.actual_media_start_seconds)} → ${clock(job.value.actual_media_end_seconds)}`)
const padding = computed(() => {
  const j = job.value
  if (!j || j.padding_before_applied_seconds === null) return '—'
  return `${Math.round(j.padding_before_applied_seconds ?? 0)} s before · ${Math.round(j.padding_after_applied_seconds ?? 0)} s after`
})
const blocked = computed(() => props.moment.timeline.find(step => step.stage === 'BLOCKED'))
</script>

<template>
  <article class="surface p-4 sm:p-5">
    <header class="flex flex-wrap items-start justify-between gap-3">
      <div class="min-w-0">
        <p class="kicker">Moment {{ moment.moment_index }} · {{ MOMENT_CATEGORIES[moment.category] ?? humanise(moment.category) }}</p>
        <p class="mt-1 text-sm text-muted">
          <AppIcon name="users" :size="13" class="-mt-0.5 inline" />
          {{ moment.positive_speakers.join(', ') }}
          <span v-if="moment.source === 'legacy_v5_import'" class="badge-quiet ml-1">Imported from V5</span>
        </p>
      </div>
      <StatusBadge :label="status.label" :tone="status.tone" :title="job?.status ?? undefined" />
    </header>

    <blockquote class="mt-3 border-l-4 border-brand-300 pl-3 text-sm leading-6 text-ink break-words">
      “{{ moment.positive_quote }}”
    </blockquote>

    <button type="button" class="btn-quiet btn-sm mt-2" @click="showDialogue = !showDialogue">
      <AppIcon :name="showDialogue ? 'chevronDown' : 'chevronRight'" :size="14" />
      {{ showDialogue ? 'Hide' : 'Show' }} dialogue ({{ moment.dialogue.length }} lines)
    </button>
    <ol v-if="showDialogue" class="mt-2 space-y-1.5 text-sm">
      <li
        v-for="line in moment.dialogue"
        :key="line.cue_index"
        class="rounded-md px-2 py-1 break-words"
        :class="line.positive ? 'bg-emerald-50' : 'bg-canvas'"
      >
        <span class="font-semibold text-ink">{{ line.speaker ?? 'Unknown' }}</span>
        <span class="ml-1 text-2xs uppercase tracking-kicker text-faint">{{ line.role }}</span>
        <span class="ml-1 text-2xs text-faint tabular-nums">{{ clock(line.start_ms / 1000) }}</span>
        <p class="text-body">{{ line.text }}</p>
      </li>
    </ol>

    <dl class="mt-4 grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
      <div class="flex justify-between gap-3"><dt class="text-muted">Evidence (transcript)</dt><dd class="tabular-nums">{{ evidence }}</dd></div>
      <div class="flex justify-between gap-3"><dt class="text-muted">Clip requested (recording)</dt><dd class="tabular-nums">{{ requested }}</dd></div>
      <div class="flex justify-between gap-3"><dt class="text-muted">Clip planned</dt><dd class="tabular-nums">{{ actual }}</dd></div>
      <div class="flex justify-between gap-3"><dt class="text-muted">Context padding</dt><dd>{{ padding }}</dd></div>
      <div class="flex justify-between gap-3">
        <dt class="text-muted">Alignment</dt>
        <dd>
          {{ humanise(job?.alignment_status ?? 'NOT_ATTEMPTED') }}
          <span v-if="job?.alignment_confidence" class="text-2xs text-faint">· {{ formatConfidence(job.alignment_confidence) }}</span>
        </dd>
      </div>
      <div class="flex justify-between gap-3">
        <dt class="text-muted">AI verification</dt>
        <dd>{{ humanise(moment.verifier_verdict) }}<span v-if="moment.verifier_confidence !== null" class="text-2xs text-faint"> · {{ formatConfidence(moment.verifier_confidence) }}</span></dd>
      </div>
    </dl>

    <p v-if="blocked" class="notice-warn mt-3" role="note">
      <AppIcon name="info" :size="16" class="mt-0.5" />
      <span>
        <strong class="font-semibold">{{ blocked.label }}:</strong> {{ humanise(blocked.detail) }}
        <template v-if="job?.error_message"> — {{ job.error_message }}</template>
        <template v-if="blocked.next_retry_at"> · next automatic retry {{ cairoDateTime(blocked.next_retry_at) }}</template>
      </span>
    </p>

    <div v-if="moment.asset" class="notice mt-3">
      <AppIcon name="checkCircle" :size="16" class="mt-0.5 text-emerald-700" />
      <span class="min-w-0 break-words">
        <strong class="font-semibold">Clip ready</strong> · {{ moment.asset.output_filename }}
        · {{ clock(moment.asset.duration_seconds) }} · delivered {{ cairoDateTime(moment.asset.completed_at) }}
      </span>
    </div>

    <div class="mt-4 flex flex-wrap gap-2">
      <button v-if="moment.asset" type="button" class="btn-primary btn-sm" @click="emit('open-clip', moment.asset.asset_id)">
        <AppIcon name="external" :size="14" /> Open clip in SharePoint
      </button>
      <button v-if="canRender" type="button" class="btn-primary btn-sm" :disabled="busy" @click="emit('render', moment.moment_id)">
        <AppIcon name="video" :size="14" /> Render
        <span v-if="job?.estimated_credits" class="font-normal opacity-80">(~{{ job.estimated_credits.toFixed(0) }} credits est.)</span>
      </button>
      <button v-if="canRetry" type="button" class="btn-secondary btn-sm" :disabled="busy" @click="emit('retry', moment.moment_id)">
        <AppIcon name="refresh" :size="14" /> Retry
      </button>
      <button type="button" class="btn-quiet btn-sm" @click="emit('open-recording')">
        <AppIcon name="play" :size="14" /> Open source recording
      </button>
      <button type="button" class="btn-quiet btn-sm" @click="showTimeline = !showTimeline">
        <AppIcon name="clock" :size="14" /> History
      </button>
    </div>

    <ol v-if="showTimeline" class="mt-3 space-y-1 text-sm">
      <li v-for="step in moment.timeline" :key="step.stage" class="flex items-center justify-between gap-3">
        <span class="inline-flex items-center gap-1.5" :class="step.done ? 'text-ink' : 'text-faint'">
          <AppIcon :name="step.done ? 'checkCircle' : 'clock'" :size="14" :class="step.done ? 'text-emerald-700' : ''" />
          {{ step.label }}
        </span>
        <span class="text-2xs text-faint">{{ step.at ? cairoDateTime(step.at) : '' }}</span>
      </li>
    </ol>
  </article>
</template>
