<script setup lang="ts">
/**
 * The evidence found in one analysed lecture.
 *
 * Each card answers four questions: what was the valuable moment, who said
 * it, why does it matter, and can I watch it. Accepted evidence - Positive
 * Moments and OFSTED Case Studies - is the page; candidates the verifier
 * rejected stay reachable behind an explicit filter, never mixed in.
 *
 * This is a FEATURE page, not a second lecture detail: it is reached from
 * Positive Moments, keyed by the legacy analysis identifier, and says nothing
 * about the processing pipeline (that lives at /lectures/:lectureId).
 *
 * Nothing here fabricates media or words. The only playable links are the
 * stored recording and produced clips; a moment with no stored quotation says
 * so. Every classification arrives from the API; the AI's own diagnostics
 * stay available in a collapsed section on each card.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppIcon from '../components/AppIcon.vue'
import ClipEvidenceDetails from '../components/ClipEvidenceDetails.vue'
import ClipRequestButton from '../components/ClipRequestButton.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import LoadingSkeleton from '../components/LoadingSkeleton.vue'
import PageHeader from '../components/PageHeader.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { getLecture, getWatchUrl } from '../services/api'
import type { DialogueLine, LectureDetail, PositiveClip } from '../types'
import { businessDate, cairoDateTime } from '../utils/datetime'
import { MOMENT_CATEGORIES, evidenceType, humanise } from '../utils/labels'
import { errorMessage, formatConfidence, openSafely } from '../utils/format'

const route = useRoute()
const router = useRouter()
const lecture = ref<LectureDetail | null>(null)
const loading = ref(true)
const error = ref('')
const opening = ref<number | null>(null)
const watchError = ref('')

const sessionKey = computed(() => String(route.params.sessionKey))

async function load() {
  loading.value = true
  error.value = ''
  try {
    lecture.value = await getLecture(sessionKey.value)
  } catch (caught) {
    error.value = errorMessage(caught, 'These positive moments could not be loaded.')
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function watchMoment(index: number) {
  if (!lecture.value?.recording_available) return
  opening.value = index
  watchError.value = ''
  try {
    openSafely(await getWatchUrl(lecture.value.session_key, index))
  } catch (caught) {
    watchError.value = errorMessage(caught, 'This moment could not be opened in the recording.')
  } finally {
    opening.value = null
  }
}

function line(item: DialogueLine | string) {
  if (typeof item === 'string') return { speaker: '', text: item, start: '' }
  return { speaker: item.speaker ?? '', text: item.text ?? item.quote ?? '', start: item.start ?? '' }
}

/** The stored dialogue, else the stored quotation. Never a made-up line. */
function transcript(clip: PositiveClip) {
  const lines = clip.dialogue.map(line).filter((entry) => entry.text.trim())
  if (lines.length) return lines
  const stored = clip.positive_quote || clip.quote
  return stored ? [{ speaker: clip.speaker ?? '', text: stored, start: clip.start ?? '' }] : []
}

/** Transcript offsets are cue timestamps, not wall-clock times. */
function offset(value: string) {
  return value ? value.replace(/\.\d+$/, '') : '—'
}

function clipDuration(value: number) {
  if (!Number.isFinite(value)) return ''
  if (value < 60) return `${Math.round(value)}s`
  return `${Math.floor(value / 60)}m ${String(Math.round(value % 60)).padStart(2, '0')}s`
}

// --- evidence ---------------------------------------------------------------
// Rejected = the verifier refused it as evidence, by type or by verdict. Both
// are read from the API; nothing is reclassified here.
const isRejected = (clip: PositiveClip) =>
  clip.final_evidence_type === 'REJECT' || clip.semantic_status === 'rejected'
const isCaseStudy = (clip: PositiveClip) => clip.final_evidence_type === 'OFSTED_CASE_STUDY'

/** The selection rationale without the workflow's "TYPE:" prefix. */
function rationale(clip: PositiveClip) {
  return (clip.reason ?? '').replace(/^\s*(POSITIVE_MOMENT|OFSTED_CASE_STUDY|REJECT)\s*:\s*/i, '').trim()
}

const indexed = computed(() => (lecture.value?.clips ?? []).map((clip, index) => ({ clip, index })))
const accepted = computed(() => indexed.value.filter(({ clip }) => !isRejected(clip)))
const rejectedClips = computed(() => indexed.value.filter(({ clip }) => isRejected(clip)))

function fromQuery(name: string) {
  return typeof route.query[name] === 'string' ? String(route.query[name]) : ''
}
const evidence = ref(fromQuery('evidence'))
const category = ref(fromQuery('category'))
watch([evidence, category], ([evidenceValue, categoryValue]) => {
  void router.replace({ query: {
    ...route.query, evidence: evidenceValue || undefined, category: categoryValue || undefined,
  } })
})

const EVIDENCE_FILTERS = [
  { value: '', label: 'All accepted evidence' },
  { value: 'POSITIVE_MOMENT', label: 'Positive Moments' },
  { value: 'OFSTED_CASE_STUDY', label: 'OFSTED Case Studies' },
]
const showingRejected = computed(() => evidence.value === 'rejected')

/** The evidence in view before the category filter. */
const scoped = computed(() => {
  if (showingRejected.value) return rejectedClips.value
  if (!evidence.value) return accepted.value
  return accepted.value.filter(({ clip }) => clip.final_evidence_type === evidence.value)
})
const evidenceCount = (value: string) => (value
  ? accepted.value.filter(({ clip }) => clip.final_evidence_type === value).length
  : accepted.value.length)

const categoryOf = (clip: PositiveClip) => (clip.category ?? '').trim().toLowerCase()
const categoryLabel = (value: string) => MOMENT_CATEGORIES[value] ?? humanise(value)
const categoryCounts = computed(() => {
  const counts = new Map<string, number>()
  for (const { clip } of scoped.value) {
    const key = categoryOf(clip)
    if (key) counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
})

const visibleClips = computed(() => scoped.value
  .filter(({ clip }) => !category.value || categoryOf(clip) === category.value))

const readyClips = computed(() => accepted.value.filter(({ clip }) => clip.clip_asset).length)

function clearFilters() {
  evidence.value = ''
  category.value = ''
}
</script>

<template>
  <div class="page stack">
    <RouterLink class="btn-quiet" :to="{ name: 'positive-moments', query: category ? { category } : {} }">
      <AppIcon name="chevronLeft" :size="14" /> Positive Moments
    </RouterLink>

    <LoadingSkeleton v-if="loading" variant="cards" :rows="3" />
    <ErrorState v-else-if="error" :message="error" @retry="load" />

    <template v-else-if="lecture">
      <PageHeader kicker="Positive Moments" :title="lecture.subject || 'Untitled lecture'">
        <template #meta>
          <div class="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-sm text-muted">
            <span class="inline-flex items-center gap-1.5">
              <AppIcon name="users" :size="15" />{{ lecture.trainer || 'Trainer not recorded' }}
            </span>
            <span class="inline-flex items-center gap-1.5">
              <AppIcon name="calendar" :size="15" />{{ businessDate(lecture.date) }}
            </span>
            <span v-if="lecture.clips_analyzed_at" class="inline-flex items-center gap-1.5">
              <AppIcon name="sparkle" :size="15" />Analysed {{ cairoDateTime(lecture.clips_analyzed_at) }}
            </span>
          </div>
          <div class="mt-3 flex flex-wrap gap-2" data-test="header-badges">
            <StatusBadge :label="`${accepted.length} accepted moment${accepted.length === 1 ? '' : 's'}`" tone="brand" />
            <StatusBadge
              :label="readyClips ? `${readyClips} of ${accepted.length} clips produced` : 'No clips produced yet'"
              :tone="readyClips ? 'ok' : 'waiting'"
            />
            <StatusBadge
              :label="lecture.recording_available ? 'Recording available' : 'Recording link pending'"
              :tone="lecture.recording_available ? 'ok' : 'waiting'"
            />
          </div>
        </template>
        <template #actions>
          <button
            type="button"
            class="btn-primary"
            :disabled="!lecture.recording_available"
            @click="lecture.recording_url && openSafely(lecture.recording_url)"
          >
            <AppIcon name="play" :size="16" />
            {{ lecture.recording_available ? 'Watch full lecture' : 'Recording link pending' }}
          </button>
        </template>
      </PageHeader>

      <p v-if="watchError" class="notice-error" role="alert">
        <AppIcon name="alert" :size="16" class="mt-0.5" />{{ watchError }}
      </p>

      <EmptyState
        v-if="!lecture.clips.length"
        class="surface"
        tone="neutral"
        icon="quote"
        title="No positive moments in this lecture"
        message="The analysis completed successfully and found nothing that qualified. That is a result, not a failure."
      />

      <div v-else class="stack">
        <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div class="flex flex-wrap items-center gap-2" role="group" aria-label="Filter by evidence type">
            <span class="filter-label">Evidence</span>
            <button
              v-for="option in EVIDENCE_FILTERS" :key="option.value" type="button" class="chip cursor-pointer transition"
              :data-test="`evidence-filter-${option.value || 'all'}`"
              :class="{ '!bg-brand-600 !text-white': evidence === option.value }" :aria-pressed="evidence === option.value"
              @click="evidence = option.value; category = ''"
            >
              {{ option.label }}<span class="ml-1" :class="evidence === option.value ? 'text-white/80' : 'text-brand-500'">{{ evidenceCount(option.value) }}</span>
            </button>
          </div>
          <button
            v-if="rejectedClips.length" type="button" data-test="evidence-filter-rejected"
            class="btn-quiet btn-sm self-start sm:self-auto" :aria-pressed="showingRejected"
            @click="evidence = showingRejected ? '' : 'rejected'; category = ''"
          >
            {{ showingRejected ? 'Back to accepted evidence' : `Show rejected candidates (${rejectedClips.length})` }}
          </button>
        </div>

        <p v-if="showingRejected" class="notice-warn" role="note" data-test="rejected-notice">
          <AppIcon name="info" :size="16" class="mt-0.5" />
          <span>These candidates were <strong>rejected as evidence</strong> by the AI verifier. They are kept for reference and are not Positive Moments or OFSTED Case Studies.</span>
        </p>

        <div v-if="categoryCounts.length > 1" class="flex flex-wrap items-center gap-2" role="group" aria-label="Filter moments by category">
          <span class="filter-label">Category</span>
          <button
            type="button" class="chip cursor-pointer transition"
            :class="{ '!bg-brand-600 !text-white': !category }" :aria-pressed="!category"
            @click="category = ''"
          >
            All<span class="ml-1" :class="!category ? 'text-white/80' : 'text-brand-500'">{{ scoped.length }}</span>
          </button>
          <button
            v-for="[value, count] in categoryCounts" :key="value" type="button" class="chip cursor-pointer transition"
            :class="{ '!bg-brand-600 !text-white': category === value }" :aria-pressed="category === value"
            @click="category = category === value ? '' : value"
          >
            {{ categoryLabel(value) }}<span class="ml-1" :class="category === value ? 'text-white/80' : 'text-brand-500'">{{ count }}</span>
          </button>
        </div>

        <EmptyState
          v-if="!visibleClips.length"
          class="surface"
          tone="neutral"
          icon="quote"
          :title="showingRejected ? 'No rejected candidates' : 'No accepted evidence matches these filters'"
          :message="accepted.length ? 'This lecture\'s evidence is of other kinds.' : 'Every candidate in this lecture was rejected as evidence.'"
        >
          <template #action><button v-if="evidence || category" type="button" class="btn-secondary" @click="clearFilters">Show all accepted evidence</button></template>
        </EmptyState>

        <article
          v-for="{ clip, index } in visibleClips"
          :key="`${clip.start}-${index}`"
          class="panel"
          data-test="evidence-card"
        >
          <div class="panel-head">
            <div class="flex min-w-0 flex-wrap items-center gap-2">
              <StatusBadge
                :label="isRejected(clip) ? 'Rejected as evidence' : evidenceType(clip.final_evidence_type).label"
                :tone="isRejected(clip) ? 'review' : evidenceType(clip.final_evidence_type).tone"
                data-test="evidence-type"
              />
              <span class="badge-neutral font-mono" data-test="timestamps">{{ offset(clip.start ?? '') }} – {{ offset(clip.end ?? '') }}</span>
              <span v-if="typeof clip.duration_seconds === 'number'" class="text-xs text-muted">{{ clipDuration(clip.duration_seconds) }}</span>
              <span v-if="clip.category" class="chip">{{ categoryLabel(categoryOf(clip)) }}</span>
            </div>
            <StatusBadge
              :label="clip.clip_asset ? `Clip ready · ${clipDuration(clip.clip_asset.duration_seconds)}` : 'Clip not produced'"
              :tone="clip.clip_asset ? 'ok' : 'waiting'"
            />
          </div>

          <div class="panel-body">
            <p v-if="isCaseStudy(clip)" class="eyebrow mb-1.5" data-test="quote-label">Case study explanation, as said in the lecture</p>
            <blockquote v-if="clip.positive_quote || clip.quote" class="max-w-4xl border-l-[3px] border-brand-300 pl-4">
              <p class="font-display text-lg leading-relaxed text-ink" data-test="quotation">
                “{{ clip.positive_quote || clip.quote }}”
              </p>
              <footer v-if="clip.speaker" class="mt-1.5 text-xs font-semibold uppercase tracking-kicker text-muted" data-test="speaker">
                {{ clip.speaker }}
              </footer>
            </blockquote>
            <p v-else class="text-sm text-muted" data-test="no-quotation">No quotation stored for this candidate.</p>

            <p v-if="isRejected(clip) && clip.semantic_reason" class="mt-3 max-w-4xl text-sm leading-6 text-muted" data-test="rejection-reason">
              <span class="font-semibold text-body">Why it was rejected:</span> {{ clip.semantic_reason }}
            </p>
            <p v-else-if="rationale(clip)" class="mt-3 max-w-4xl text-sm leading-6 text-muted" data-test="rationale">
              <span class="font-semibold text-body">Why it matters:</span> {{ rationale(clip) }}
            </p>

            <div class="mt-4 flex flex-wrap gap-2">
              <button
                type="button"
                class="btn-secondary btn-sm"
                :disabled="!lecture.recording_available || opening === index"
                @click="watchMoment(index)"
              >
                <AppIcon name="play" :size="14" />
                {{ !lecture.recording_available ? 'Recording link pending'
                  : opening === index ? 'Opening…' : 'Watch in full lecture' }}
              </button>
              <a
                v-if="clip.clip_asset"
                class="btn-secondary btn-sm"
                :href="clip.clip_asset.url"
                target="_blank"
                rel="noopener noreferrer"
              ><AppIcon name="external" :size="14" /> View clip</a>
              <ClipRequestButton
                v-else-if="typeof clip.start_cue === 'number' && typeof clip.end_cue === 'number'"
                :session-key="lecture.session_key"
                :start-cue="clip.start_cue"
                :end-cue="clip.end_cue"
              />
            </div>

            <details class="group mt-4 rounded-lg border border-line bg-[#fbfafc]">
              <summary class="flex cursor-pointer list-none items-center gap-2 px-3.5 py-2.5 text-xs font-semibold text-muted hover:text-brand-700">
                <AppIcon name="chevronRight" :size="14" class="transition group-open:rotate-90" />
                Transcript around this moment
              </summary>
              <div class="space-y-2 border-t border-line px-3.5 py-3">
                <div
                  v-for="(entry, position) in transcript(clip)"
                  :key="`${entry.start}-${position}`"
                  class="grid gap-1 sm:grid-cols-[5.5rem_1fr] sm:gap-4"
                >
                  <time class="font-mono text-2xs tabular-nums text-brand-600">{{ offset(entry.start) }}</time>
                  <div class="min-w-0">
                    <p v-if="entry.speaker" class="text-xs font-bold text-ink">{{ entry.speaker }}</p>
                    <p class="text-sm leading-6 text-body">{{ entry.text }}</p>
                  </div>
                </div>
                <p v-if="!transcript(clip).length" class="text-sm text-muted">No transcript lines stored for this candidate.</p>
              </div>
            </details>

            <details class="group mt-2 rounded-lg border border-line" data-test="ai-details">
              <summary class="flex cursor-pointer list-none items-center gap-2 px-3.5 py-2.5 text-xs font-semibold text-muted hover:text-brand-700">
                <AppIcon name="chevronRight" :size="14" class="transition group-open:rotate-90" />
                AI analysis details
              </summary>
              <div class="border-t border-line px-3.5 pb-3">
                <p v-if="typeof clip.confidence === 'number'" class="mt-3 text-sm text-muted">
                  <span class="font-semibold text-body">Selector confidence:</span> {{ formatConfidence(clip.confidence) }}
                </p>
                <ClipEvidenceDetails :clip="clip" />
              </div>
            </details>
          </div>
        </article>
      </div>
    </template>
  </div>
</template>
