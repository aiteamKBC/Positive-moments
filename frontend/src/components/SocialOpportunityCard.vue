<script setup lang="ts">
/**
 * One AI-suggested social media opportunity, exactly as saved.
 *
 * Compact by default so a lecture's dozens of opportunities can be scanned:
 * primary format, alternative formats, heading, speaker, times and the video
 * actions. Everything else - the full quote, the AI's reasoning, slide
 * outline, reel segments and the raw saved data - sits behind View details.
 *
 * Two different videos can exist for one opportunity, and they are never
 * confused:
 *   exact reel      `clip` - this opportunity's own Social Media render, cut to
 *                   its exact saved times;
 *   existing clip   `evidence_clip` - the Positive Moments / OFSTED clip of the
 *                   same moment, which carries context before and after.
 * Presentation only. Nothing here approves, schedules or publishes.
 */
import { computed } from 'vue'

import AppIcon from './AppIcon.vue'
import SocialClipButton from './SocialClipButton.vue'
import StatusBadge from './StatusBadge.vue'
import type { SocialClip, SocialOpportunity, SocialReelSegment } from '../types/social'
import { evidenceType, humanise } from '../utils/labels'
import {
  DISCOVERY_SOURCES,
  FORMAT_STYLE,
  SOCIAL_FORMAT_LABELS,
  contextLabel,
  offset,
  opportunityHeading,
  seconds,
} from '../utils/social'
import { openSafely } from '../utils/format'

const props = defineProps<{ opportunity: SocialOpportunity; open: boolean; sessionKey: string }>()
const emit = defineEmits<{ toggle: []; clip: [clip: SocialClip] }>()

const detailsId = computed(() => `opportunity-details-${props.opportunity.opportunity_id}`)
const formatLabel = (value: string) => SOCIAL_FORMAT_LABELS[value] ?? humanise(value)
// The saved content angle, or a display-only fallback; the stored value is untouched.
const heading = computed(() => opportunityHeading(props.opportunity))
const style = computed(() => FORMAT_STYLE[props.opportunity.primary_format ?? '']
  ?? { accent: 'border-l-line', icon: 'sparkle' })
const alternatives = computed(() => props.opportunity.formats.filter((format) => format !== props.opportunity.primary_format))
const source = computed(() => DISCOVERY_SOURCES[props.opportunity.discovery_source]
  ?? { label: humanise(props.opportunity.discovery_source), tone: 'neutral' as const })
const hasReel = computed(() => props.opportunity.formats.includes('reel') || props.opportunity.reel_segments.length > 0)
const hasCarousel = computed(() => props.opportunity.formats.includes('carousel')
  || props.opportunity.carousel_slide_outline.length > 0)
const payload = computed(() => JSON.stringify(props.opportunity.opportunity_data, null, 2))

const exactReel = computed(() => (props.opportunity.clip?.state === 'ready' && props.opportunity.clip.clip_url
  ? props.opportunity.clip : null))
const existing = computed(() => props.opportunity.evidence_clip)
const existingKind = computed(() => (existing.value?.evidence_type === 'OFSTED_CASE_STUDY'
  ? 'OFSTED Case Study clip' : 'Positive Moments clip'))
const existingContext = computed(() => (existing.value ? contextLabel(existing.value) : ''))

/** The selection note and the verifier's note, said once each. */
const normalise = (value?: string | null) => (value ?? '').replace(/\s+/g, ' ').trim().toLowerCase()
const verification = computed(() => {
  const note = props.opportunity.verification_reason
  return note && normalise(note) !== normalise(props.opportunity.why_selected) ? note : null
})

function segmentText(segment: SocialReelSegment) {
  const cues = `cues ${segment.start_cue ?? '?'}–${segment.end_cue ?? '?'}`
  if (!segment.start && !segment.end) return cues
  return `${cues} · ${offset(segment.start)} – ${offset(segment.end)}`
}
</script>

<template>
  <article
    class="surface overflow-hidden border-l-4" :class="style.accent"
    :data-format="opportunity.primary_format" data-test="social-card"
  >
    <div class="flex flex-col gap-3 px-4 py-3 sm:px-5 lg:flex-row lg:items-start lg:gap-6">
      <div class="min-w-0 flex-1 space-y-1.5">
        <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
          <span class="inline-flex items-center gap-1 font-bold uppercase tracking-kicker text-ink" data-test="primary-format">
            <AppIcon :name="style.icon" :size="14" />{{ formatLabel(opportunity.primary_format ?? '') }}
          </span>
          <span v-if="alternatives.length" class="inline-flex flex-wrap items-center gap-1" data-test="alternative-formats">
            <span class="text-faint">Also:&#32;</span>
            <span v-for="format in alternatives" :key="format" class="chip !py-0 !text-2xs" data-test="format">{{ formatLabel(format) }}</span>
          </span>
        </div>

        <h3
          class="text-[0.9375rem] font-semibold leading-snug text-ink" :class="{ 'line-clamp-2': !open }"
          :data-heading-source="heading.source" data-test="content-angle"
        >
          {{ heading.text }}
        </h3>

        <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
          <span v-if="opportunity.speaker" class="inline-flex min-w-0 items-center gap-1" data-test="speaker">
            <AppIcon name="users" :size="13" /><span class="truncate">{{ opportunity.speaker }}</span>
          </span>
          <span class="font-mono tabular-nums text-body" data-test="timestamps">
            {{ offset(opportunity.start_time) }} – {{ offset(opportunity.end_time) }}
          </span>
          <span v-if="seconds(opportunity.duration_seconds)">{{ seconds(opportunity.duration_seconds) }}</span>
        </div>
      </div>

      <div class="flex flex-wrap items-center gap-2 lg:max-w-[26rem] lg:shrink-0 lg:justify-end" data-test="video-actions">
        <button
          v-if="opportunity.watch_url" type="button" class="btn-secondary btn-sm" data-test="watch"
          :title="`Open the original recording at ${offset(opportunity.start_time)}`"
          @click="openSafely(opportunity.watch_url)"
        >
          <AppIcon name="play" :size="14" /> Open Recording
        </button>
        <template v-if="existing && !exactReel">
          <a
            class="btn-secondary btn-sm" :href="existing.url" target="_blank" rel="noopener noreferrer"
            :title="`The ${existingKind} of this moment. ${existingContext}.`" data-test="view-existing-clip"
          ><AppIcon name="external" :size="14" /> View Existing Clip</a>
          <span class="badge-quiet" data-test="existing-clip-context">{{ existingContext }}</span>
        </template>
        <SocialClipButton
          v-if="opportunity.clip" :session-key="sessionKey" :opportunity-id="opportunity.opportunity_id"
          :clip="opportunity.clip" :has-existing-clip="Boolean(existing)" @update="emit('clip', $event)"
        />
        <button
          type="button" class="btn-quiet btn-sm" data-test="toggle-details"
          :aria-expanded="open" :aria-controls="detailsId"
          :aria-label="`${open ? 'Hide' : 'View'} details: ${heading.text}`" @click="emit('toggle')"
        >
          {{ open ? 'Hide details' : 'View details' }}
          <AppIcon name="chevronDown" :size="14" class="transition" :class="{ 'rotate-180': open }" />
        </button>
      </div>
    </div>

    <div v-if="open" :id="detailsId" class="space-y-4 border-t border-line bg-[#fbfafc] px-4 py-4 sm:px-5" data-test="details">
      <blockquote v-if="opportunity.quote" class="max-w-4xl border-l-[3px] border-brand-300 pl-4">
        <p class="text-sm leading-relaxed text-ink" data-test="quote">“{{ opportunity.quote }}”</p>
        <footer v-if="opportunity.speaker" class="mt-1 text-2xs font-semibold uppercase tracking-kicker text-muted">
          {{ opportunity.speaker }}
        </footer>
      </blockquote>
      <p v-else class="text-sm text-muted">No quotation stored for this opportunity.</p>

      <dl class="grid gap-x-6 gap-y-2 text-sm leading-6 md:grid-cols-2">
        <div v-if="opportunity.why_selected" data-test="why-selected">
          <dt class="eyebrow">Why it was selected</dt>
          <dd class="text-body">{{ opportunity.why_selected }}</dd>
        </div>
        <div v-if="verification" data-test="verification">
          <dt class="eyebrow">AI verification</dt>
          <dd class="text-body">{{ verification }}</dd>
        </div>
        <div>
          <dt class="eyebrow">Source</dt>
          <dd class="mt-0.5 flex flex-wrap gap-1.5">
            <StatusBadge :label="source.label" :tone="source.tone" />
            <StatusBadge
              v-if="opportunity.evidence_type"
              :label="evidenceType(opportunity.evidence_type).label" :tone="evidenceType(opportunity.evidence_type).tone"
            />
          </dd>
        </div>
        <div v-if="opportunity.clip?.state === 'unavailable' && opportunity.clip.reason" data-test="clip-note">
          <dt class="eyebrow">Clip</dt>
          <dd class="text-muted">{{ opportunity.clip.reason }}</dd>
        </div>
        <div v-if="heading.source !== 'content_angle'" data-test="heading-note">
          <dt class="eyebrow">Heading</dt>
          <dd class="text-muted">
            No content angle was saved for this opportunity. The heading is
            {{ heading.source === 'format' ? 'its format.' : `taken from its saved ${heading.source === 'quote' ? 'quote' : 'selection note'}.` }}
          </dd>
        </div>
      </dl>

      <section v-if="exactReel || existing" class="rounded-lg border border-line bg-white px-3.5 py-3 text-sm" data-test="videos">
        <p class="flex items-center gap-1.5 text-xs font-bold text-ink"><AppIcon name="video" :size="14" /> Videos</p>
        <ul class="mt-1.5 space-y-1.5">
          <li v-if="exactReel" class="flex flex-wrap items-center gap-2" data-test="video-exact">
            <a class="font-semibold text-brand-700 underline" :href="exactReel.clip_url!" target="_blank" rel="noopener noreferrer">Exact reel</a>
            <span class="text-xs text-muted">Social Media render of exactly {{ offset(opportunity.start_time) }} – {{ offset(opportunity.end_time) }}, no added context.</span>
          </li>
          <li v-if="existing" class="flex flex-wrap items-center gap-2" data-test="video-existing">
            <a class="font-semibold text-brand-700 underline" :href="existing.url" target="_blank" rel="noopener noreferrer">{{ existingKind }}</a>
            <span class="text-xs text-muted">The existing evidence clip of this moment · {{ existingContext }} · {{ seconds(existing.duration_seconds) }}.</span>
          </li>
        </ul>
      </section>

      <div v-if="hasReel || hasCarousel" class="grid gap-3 md:grid-cols-2">
        <section v-if="hasReel" class="rounded-lg border border-line bg-white px-3.5 py-3 text-sm" data-test="reel">
          <p class="flex items-center gap-1.5 text-xs font-bold text-ink"><AppIcon name="video" :size="14" /> Reel</p>
          <p class="mt-0.5 text-xs text-muted">
            <span v-if="opportunity.reel_edit_mode">Edit: {{ humanise(opportunity.reel_edit_mode) }}</span>
            <span v-if="typeof opportunity.reel_estimated_duration_seconds === 'number'"> · about {{ seconds(opportunity.reel_estimated_duration_seconds) }}</span>
          </p>
          <ol v-if="opportunity.reel_segments.length" class="mt-1 space-y-0.5">
            <li
              v-for="(segment, position) in opportunity.reel_segments" :key="position"
              class="font-mono text-2xs text-body" data-test="reel-segment"
            >
              {{ position + 1 }}. {{ segmentText(segment) }}
            </li>
          </ol>
          <p v-else class="mt-0.5 text-xs text-muted">No reel segments stored.</p>
        </section>

        <section v-if="hasCarousel" class="rounded-lg border border-line bg-white px-3.5 py-3 text-sm" data-test="carousel">
          <p class="flex items-center gap-1.5 text-xs font-bold text-ink"><AppIcon name="layers" :size="14" /> Carousel slide outline</p>
          <ol v-if="opportunity.carousel_slide_outline.length" class="mt-1 list-decimal space-y-0.5 pl-5">
            <li
              v-for="(slide, position) in opportunity.carousel_slide_outline" :key="position"
              class="text-body" data-test="carousel-slide"
            >
              {{ slide }}
            </li>
          </ol>
          <p v-else class="mt-0.5 text-xs text-muted">No slide outline stored.</p>
        </section>
      </div>

      <details class="group rounded-lg border border-line bg-white">
        <summary class="flex cursor-pointer list-none items-center gap-2 px-3.5 py-2 text-xs font-semibold text-muted hover:text-brand-700">
          <AppIcon name="chevronRight" :size="14" class="transition group-open:rotate-90" />
          Saved opportunity data
        </summary>
        <pre class="max-h-72 overflow-auto border-t border-line px-3.5 py-3 font-mono text-2xs leading-5 text-body" data-test="payload">{{ payload }}</pre>
      </details>
    </div>
  </article>
</template>
