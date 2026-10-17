<script setup lang="ts">
/**
 * The moments found in one analysed lecture.
 *
 * This is a FEATURE page, not a second lecture detail: it is reached from
 * Positive Moments, it is keyed by the legacy analysis identifier, and it says
 * nothing about the processing pipeline. The canonical operational view of a
 * lecture lives at /lectures/:lectureId and is the only place that describes
 * stages, retries and sync.
 *
 * Nothing here fabricates media or words. A moment with no produced clip says
 * so, the only playable links are the durable stored SharePoint URLs the
 * platform already holds, and a clip with no stored quotation says that
 * instead of showing one.
 *
 * Every classification - evidence type, AI verdict, social formats - arrives
 * from the API. The filters here only select on it.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppIcon from '../components/AppIcon.vue'
import ClipEvidenceDetails from '../components/ClipEvidenceDetails.vue'
import ClipRequestButton from '../components/ClipRequestButton.vue'
import ClipReviewControl from '../components/ClipReviewControl.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import LoadingSkeleton from '../components/LoadingSkeleton.vue'
import PageHeader from '../components/PageHeader.vue'
import StatTile from '../components/StatTile.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { getLecture, getWatchUrl } from '../services/api'
import type { ClipReview, DialogueLine, LectureDetail, PositiveClip } from '../types'
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

const isCaseStudy = (clip: PositiveClip) => clip.final_evidence_type === 'OFSTED_CASE_STUDY'

function onReviewed(index: number, review: ClipReview) {
  const clip = lecture.value?.clips[index]
  if (clip) clip.review = review
}

const readyClips = computed(() => lecture.value?.clips.filter((clip) => clip.clip_asset).length ?? 0)
const reviewCounts = computed(() => {
  const counts = { pending: 0, approved: 0, rejected: 0 }
  for (const clip of lecture.value?.clips ?? []) if (clip.review) counts[clip.review.status] += 1
  return counts
})

// --- filters ----------------------------------------------------------------
// Three independent selections, each kept in the URL. Each moment keeps its
// original index: "Watch in full lecture" asks the API by it.
function fromQuery(name: string) {
  return typeof route.query[name] === 'string' ? String(route.query[name]) : ''
}
const category = ref(fromQuery('category'))
const evidence = ref(fromQuery('evidence'))
const verdict = ref(fromQuery('ai'))
watch([category, evidence, verdict], ([categoryValue, evidenceValue, verdictValue]) => {
  void router.replace({ query: {
    ...route.query,
    category: categoryValue || undefined, evidence: evidenceValue || undefined, ai: verdictValue || undefined,
  } })
})

const EVIDENCE_FILTERS = [
  { value: '', label: 'All evidence' },
  { value: 'POSITIVE_MOMENT', label: 'Positive Moments' },
  { value: 'OFSTED_CASE_STUDY', label: 'OFSTED Case Studies' },
  { value: 'social', label: 'Social Media candidates' },
]
const VERDICT_FILTERS = [
  { value: '', label: 'Any AI verdict' },
  { value: 'accepted', label: 'AI accepted' },
  { value: 'rejected', label: 'AI rejected' },
  { value: 'not_verified', label: 'Not verified' },
]

function matchesEvidence(clip: PositiveClip, value: string) {
  if (!value) return true
  if (value === 'social') return clip.is_social_candidate
  return clip.final_evidence_type === value
}
const matchesVerdict = (clip: PositiveClip, value: string) => !value || clip.semantic_status === value

const evidenceCount = (value: string) => (lecture.value?.clips ?? []).filter((clip) => matchesEvidence(clip, value)).length
const verdictCount = (value: string) => (lecture.value?.clips ?? []).filter((clip) => matchesVerdict(clip, value)).length

const categoryOf = (clip: PositiveClip) => (clip.category ?? '').trim().toLowerCase()
const categoryLabel = (value: string) => MOMENT_CATEGORIES[value] ?? humanise(value)

const categoryCounts = computed(() => {
  const counts = new Map<string, number>()
  for (const clip of lecture.value?.clips ?? []) {
    const key = categoryOf(clip)
    if (key) counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
})

const filtering = computed(() => Boolean(category.value || evidence.value || verdict.value))
function clearFilters() {
  category.value = ''
  evidence.value = ''
  verdict.value = ''
}

const visibleClips = computed(() => (lecture.value?.clips ?? [])
  .map((clip, index) => ({ clip, index }))
  .filter(({ clip }) => (!category.value || categoryOf(clip) === category.value)
    && matchesEvidence(clip, evidence.value) && matchesVerdict(clip, verdict.value)))
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
          <div class="mt-3 flex flex-wrap gap-2">
            <StatusBadge :label="`${lecture.clips.length} candidate${lecture.clips.length === 1 ? '' : 's'}`" tone="brand" />
            <StatusBadge
              :label="readyClips ? `${readyClips} of ${lecture.clips.length} clips produced` : 'No clips produced yet'"
              :tone="readyClips ? 'ok' : 'waiting'"
            />
            <StatusBadge
              :label="lecture.recording_available ? 'Recording available' : 'Recording link pending'"
              :tone="lecture.recording_available ? 'ok' : 'waiting'"
            />
            <StatusBadge
              v-if="lecture.clips.length"
              :label="`${reviewCounts.approved} approved · ${reviewCounts.rejected} rejected · ${reviewCounts.pending} awaiting review`"
              tone="neutral"
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
        <div v-if="lecture.evidence_summary" class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5" data-test="evidence-summary">
          <StatTile label="Candidates" :value="lecture.evidence_summary.total" />
          <StatTile label="AI accepted" :value="lecture.evidence_summary.ai_accepted" tone="ok" />
          <StatTile label="AI rejected" :value="lecture.evidence_summary.ai_rejected" tone="review" />
          <StatTile label="Positive Moments" :value="lecture.evidence_summary.positive_moments" />
          <StatTile label="OFSTED Case Studies" :value="lecture.evidence_summary.ofsted_case_studies" />
          <StatTile label="Social Media" :value="lecture.evidence_summary.social_candidates" note="candidates, each counted once" />
          <StatTile label="Reels" :value="lecture.evidence_summary.reel" />
          <StatTile label="Carousels" :value="lecture.evidence_summary.carousel" />
          <StatTile label="Quote posts" :value="lecture.evidence_summary.quote_post" />
          <StatTile label="Not verified" :value="lecture.evidence_summary.not_verified" />
        </div>

        <div class="flex flex-wrap items-center gap-2" role="group" aria-label="Filter by evidence type">
          <span class="filter-label">Evidence</span>
          <button
            v-for="option in EVIDENCE_FILTERS" :key="option.value" type="button" class="chip cursor-pointer transition"
            :data-test="`evidence-filter-${option.value || 'all'}`"
            :class="{ '!bg-brand-600 !text-white': evidence === option.value }" :aria-pressed="evidence === option.value"
            @click="evidence = option.value"
          >
            {{ option.label }}<span class="ml-1" :class="evidence === option.value ? 'text-white/80' : 'text-brand-500'">{{ evidenceCount(option.value) }}</span>
          </button>
        </div>

        <div class="flex flex-wrap items-center gap-2" role="group" aria-label="Filter by AI verdict">
          <span class="filter-label">AI verdict</span>
          <button
            v-for="option in VERDICT_FILTERS" :key="option.value" type="button" class="chip cursor-pointer transition"
            :data-test="`verdict-filter-${option.value || 'all'}`"
            :class="{ '!bg-brand-600 !text-white': verdict === option.value }" :aria-pressed="verdict === option.value"
            @click="verdict = option.value"
          >
            {{ option.label }}<span class="ml-1" :class="verdict === option.value ? 'text-white/80' : 'text-brand-500'">{{ verdictCount(option.value) }}</span>
          </button>
        </div>

        <div v-if="categoryCounts.length" class="flex flex-wrap items-center gap-2" role="group" aria-label="Filter moments by category">
          <span class="filter-label">Category</span>
          <button
            type="button" class="chip cursor-pointer transition"
            :class="{ '!bg-brand-600 !text-white': !category }" :aria-pressed="!category"
            @click="category = ''"
          >
            All<span class="ml-1" :class="!category ? 'text-white/80' : 'text-brand-500'">{{ lecture.clips.length }}</span>
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
          title="No candidates match these filters"
          message="This lecture's candidates are of other kinds."
        >
          <template #action><button v-if="filtering" type="button" class="btn-secondary" @click="clearFilters">Show all candidates</button></template>
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
                :label="evidenceType(clip.final_evidence_type).label"
                :tone="evidenceType(clip.final_evidence_type).tone"
                data-test="evidence-type"
              />
              <span class="badge-neutral font-mono">{{ offset(clip.start ?? '') }} – {{ offset(clip.end ?? '') }}</span>
              <span v-if="typeof clip.duration_seconds === 'number'" class="text-xs text-muted">{{ clipDuration(clip.duration_seconds) }}</span>
              <span v-if="clip.category" class="chip">{{ categoryLabel(categoryOf(clip)) }}</span>
              <span v-if="clip.feedback_target" class="badge-info">About: {{ humanise(clip.feedback_target) }}</span>
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
              <footer v-if="clip.speaker" class="mt-1.5 text-xs font-semibold uppercase tracking-kicker text-muted">
                {{ clip.speaker }}
              </footer>
            </blockquote>
            <p v-else class="text-sm text-muted" data-test="no-quotation">No quotation stored for this candidate.</p>

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

            <div v-if="clip.reason || typeof clip.confidence === 'number'" class="mt-3 space-y-1.5 text-sm leading-6 text-muted">
              <p v-if="clip.reason"><span class="font-semibold text-body">Why it was selected:</span> {{ clip.reason }}</p>
              <p v-if="typeof clip.confidence === 'number'"><span class="font-semibold text-body">Selector confidence:</span> {{ formatConfidence(clip.confidence) }}</p>
            </div>

            <ClipEvidenceDetails :clip="clip" />

            <ClipReviewControl
              v-if="clip.review && typeof clip.start_cue === 'number' && typeof clip.end_cue === 'number'"
              :session-key="lecture.session_key"
              :start-cue="clip.start_cue"
              :end-cue="clip.end_cue"
              :review="clip.review"
              :can-review="lecture.can_review"
              @reviewed="(review) => onReviewed(index, review)"
            />
          </div>
        </article>
      </div>
    </template>
  </div>
</template>
