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
 * Nothing here fabricates media. A moment with no produced clip says so, and
 * the only playable links are the durable stored SharePoint URLs the platform
 * already holds.
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import AppIcon from '../components/AppIcon.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import LoadingSkeleton from '../components/LoadingSkeleton.vue'
import PageHeader from '../components/PageHeader.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { getLecture, getWatchUrl } from '../services/api'
import type { DialogueLine, LectureDetail, PositiveClip } from '../types'
import { businessDate, cairoDateTime } from '../utils/datetime'
import { humanise } from '../utils/labels'
import { errorMessage, formatConfidence, openSafely } from '../utils/format'

const route = useRoute()
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

function transcript(clip: PositiveClip) {
  const lines = clip.dialogue.map(line).filter((entry) => entry.text.trim())
  if (lines.length) return lines
  return [{
    speaker: clip.speaker ?? '',
    text: clip.positive_quote || clip.quote || 'Positive learner feedback',
    start: clip.start ?? '',
  }]
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

/**
 * The verifier's own vocabulary. Only its explicit approvals read as green;
 * anything else stays neutral rather than being guessed at by substring.
 */
const APPROVED = new Set(['accept', 'accepted', 'confirmed', 'confirm', 'pass', 'passed'])
function verificationTone(verdict?: string) {
  return APPROVED.has((verdict ?? '').trim().toLowerCase()) ? 'ok' as const : 'neutral' as const
}

const readyClips = computed(() => lecture.value?.clips.filter((clip) => clip.clip_asset).length ?? 0)
</script>

<template>
  <div class="page stack">
    <RouterLink class="btn-quiet" :to="{ name: 'positive-moments' }">
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
            <StatusBadge :label="`${lecture.clips.length} positive moment${lecture.clips.length === 1 ? '' : 's'}`" tone="brand" />
            <StatusBadge
              :label="readyClips ? `${readyClips} of ${lecture.clips.length} clips produced` : 'No clips produced yet'"
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
        <article
          v-for="(clip, index) in lecture.clips"
          :key="`${clip.start}-${index}`"
          class="panel"
        >
          <div class="panel-head">
            <div class="flex min-w-0 flex-wrap items-center gap-2">
              <span class="badge-neutral font-mono">{{ offset(clip.start ?? '') }} – {{ offset(clip.end ?? '') }}</span>
              <span v-if="clip.category" class="chip">{{ humanise(clip.category) }}</span>
              <span v-if="clip.feedback_target" class="badge-info">About: {{ humanise(clip.feedback_target) }}</span>
            </div>
            <StatusBadge
              :label="clip.clip_asset ? `Clip ready · ${clipDuration(clip.clip_asset.duration_seconds)}` : 'Clip not produced'"
              :tone="clip.clip_asset ? 'ok' : 'waiting'"
            />
          </div>

          <div class="panel-body">
            <blockquote class="max-w-4xl border-l-[3px] border-brand-300 pl-4">
              <p class="font-display text-lg leading-relaxed text-ink">
                “{{ clip.positive_quote || clip.quote || 'Positive learner feedback' }}”
              </p>
              <footer v-if="clip.speaker" class="mt-1.5 text-xs font-semibold uppercase tracking-kicker text-muted">
                {{ clip.speaker }}
              </footer>
            </blockquote>

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
              ><AppIcon name="external" :size="14" /> Open the produced clip</a>
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
              </div>
            </details>

            <dl
              v-if="clip.reason || typeof clip.confidence === 'number' || clip.semantic_verification.verdict"
              class="mt-4 grid gap-x-8 gap-y-2 text-sm sm:grid-cols-3"
            >
              <div v-if="typeof clip.confidence === 'number'">
                <dt class="eyebrow">Confidence</dt>
                <dd class="mt-0.5 font-semibold text-ink">{{ formatConfidence(clip.confidence) }}</dd>
              </div>
              <div v-if="clip.semantic_verification.verdict">
                <dt class="eyebrow">Semantic check</dt>
                <dd class="mt-0.5">
                  <StatusBadge
                    :label="humanise(clip.semantic_verification.verdict)"
                    :tone="verificationTone(clip.semantic_verification.verdict)"
                  />
                </dd>
              </div>
              <div v-if="typeof clip.semantic_verification.confidence === 'number'">
                <dt class="eyebrow">Verification confidence</dt>
                <dd class="mt-0.5 font-semibold text-ink">{{ formatConfidence(clip.semantic_verification.confidence) }}</dd>
              </div>
            </dl>

            <div v-if="clip.reason || clip.semantic_verification.reason" class="mt-3 space-y-1.5 text-sm leading-6 text-muted">
              <p v-if="clip.reason"><span class="font-semibold text-body">Why it qualified:</span> {{ clip.reason }}</p>
              <p v-if="clip.semantic_verification.reason"><span class="font-semibold text-body">Verification:</span> {{ clip.semantic_verification.reason }}</p>
            </div>
          </div>
        </article>
      </div>
    </template>
  </div>
</template>
