<script setup lang="ts">
/**
 * What the workflow recorded about one clip: the advisory AI verdict and the
 * social-media formats it proposed.
 *
 * Presentation only. Every value arrives from the API as stored; a field the
 * clip does not carry is simply not shown, never shown as "no". Quotations are
 * printed exactly as stored - nothing here writes, trims or completes one.
 */
import { computed } from 'vue'

import StatusBadge from './StatusBadge.vue'
import type { PositiveClip, ReelSegment } from '../types'
import { SEMANTIC_STATUS, SOCIAL_FORMATS, evidenceType, humanise } from '../utils/labels'
import { formatConfidence } from '../utils/format'

const props = defineProps<{ clip: PositiveClip }>()

const semantic = computed(() => SEMANTIC_STATUS[props.clip.semantic_status] ?? SEMANTIC_STATUS.not_verified)
// An API older than these fields sends none of them: show nothing, not an error.
const formats = computed(() => props.clip.social_formats ?? [])
const has = (format: string) => formats.value.includes(format as never)

/** Transcript offsets are cue timestamps, not wall-clock times. */
function offset(value: string | null) {
  return value ? value.replace(/\.\d+$/, '') : null
}

/** Cue numbers always; stored times only when the clip's dialogue carries them. */
function segmentText(segment: ReelSegment) {
  const cues = `cues ${segment.start_cue ?? '?'}–${segment.end_cue ?? '?'}`
  if (!segment.start && !segment.end) return cues
  return `${cues} · ${offset(segment.start) ?? '—'} – ${offset(segment.end) ?? '—'}`
}

function seconds(value: number) {
  return value < 60 ? `${Math.round(value)}s`
    : `${Math.floor(value / 60)}m ${String(Math.round(value % 60)).padStart(2, '0')}s`
}

const readiness = computed(() => {
  if (props.clip.social_media_ready === true) return 'AI marked ready. Not approved for publishing until a reviewer approves it.'
  if (props.clip.social_media_ready === false) return 'AI marked not ready.'
  return null
})
</script>

<template>
  <div class="mt-4 space-y-4 text-sm">
    <section data-test="semantic" class="space-y-1.5">
      <div class="flex flex-wrap items-center gap-2">
        <span class="eyebrow">Semantic check</span>
        <StatusBadge :label="semantic.label" :tone="semantic.tone" :title="clip.semantic_verdict" />
        <span v-if="typeof clip.semantic_confidence === 'number'" class="text-xs text-muted">
          Confidence {{ formatConfidence(clip.semantic_confidence) }}
        </span>
        <span v-if="clip.semantic_status !== 'not_verified'" class="text-xs text-faint">Advisory only</span>
      </div>
      <p v-if="clip.semantic_reason" class="leading-6 text-muted">{{ clip.semantic_reason }}</p>
      <p v-if="clip.evidence_type_reclassified && clip.selector_evidence_type" class="text-xs text-muted" data-test="reclassified">
        The Selector proposed {{ evidenceType(clip.selector_evidence_type).label }}; the verifier classified it as
        {{ evidenceType(clip.final_evidence_type).label }}.
      </p>
    </section>

    <section v-if="clip.is_social_candidate || clip.social_media_ready !== null" data-test="social" class="space-y-2 rounded-lg border border-line px-3.5 py-3">
      <div class="flex flex-wrap items-center gap-2">
        <span class="eyebrow">Social media</span>
        <span v-for="format in formats" :key="format" class="chip" data-test="social-format">
          {{ SOCIAL_FORMATS[format] ?? humanise(format) }}<span v-if="format === clip.social_media_primary_format" class="ml-1 text-brand-500">· primary</span>
        </span>
        <span v-if="!formats.length" class="text-xs text-muted">No format proposed</span>
        <StatusBadge v-if="clip.social_verification_status" :value="clip.social_verification_status" tone="neutral" />
      </div>
      <p v-if="readiness" class="text-xs font-semibold text-body" data-test="readiness">{{ readiness }}</p>
      <p v-if="clip.social_media_rationale" class="leading-6 text-muted">{{ clip.social_media_rationale }}</p>
      <p v-if="clip.social_verifier_reason" class="leading-6 text-muted">
        <span class="font-semibold text-body">Format check:</span> {{ clip.social_verifier_reason }}
      </p>

      <div v-if="has('reel')" data-test="reel" class="pt-1">
        <p class="text-xs font-bold text-ink">Reel</p>
        <p class="mt-0.5 text-xs text-muted">
          <span v-if="clip.reel_edit_mode">Edit: {{ humanise(clip.reel_edit_mode) }}</span>
          <span v-if="typeof clip.reel_estimated_duration_seconds === 'number'"> · about {{ seconds(clip.reel_estimated_duration_seconds) }}</span>
        </p>
        <ol v-if="clip.reel_segments?.length" class="mt-1 space-y-0.5">
          <li v-for="(segment, position) in clip.reel_segments" :key="position" class="font-mono text-2xs text-body" data-test="reel-segment">
            {{ position + 1 }}. {{ segmentText(segment) }}
          </li>
        </ol>
      </div>

      <div v-if="has('carousel')" data-test="carousel" class="pt-1">
        <p class="text-xs font-bold text-ink">Carousel</p>
        <ol v-if="clip.carousel_slide_outline?.length" class="mt-1 list-decimal space-y-0.5 pl-5">
          <li v-for="(slide, position) in clip.carousel_slide_outline" :key="position" class="text-body" data-test="carousel-slide">{{ slide }}</li>
        </ol>
        <p v-else class="mt-0.5 text-xs text-muted">No slide outline stored.</p>
      </div>

      <div v-if="has('quote_post')" data-test="quote-post" class="pt-1">
        <p class="text-xs font-bold text-ink">Quote post</p>
        <p v-if="clip.positive_quote" class="mt-0.5 text-body">“{{ clip.positive_quote }}”<span v-if="clip.speaker" class="text-muted"> — {{ clip.speaker }}</span></p>
        <p v-else class="mt-0.5 text-xs text-muted">No exact quotation stored.</p>
      </div>
    </section>
  </div>
</template>
