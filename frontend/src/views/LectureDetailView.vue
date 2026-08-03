<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import ErrorState from '../components/ErrorState.vue'
import LoadingSkeleton from '../components/LoadingSkeleton.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { getLecture, getWatchUrl } from '../services/api'
import type { DialogueLine, LectureDetail, PositiveClip } from '../types'
import {
  errorMessage,
  formatConfidence,
  formatDate,
  formatStatus,
  openSafely,
} from '../utils/format'

const route = useRoute()
const lecture = ref<LectureDetail | null>(null)
const loading = ref(true)
const error = ref('')
const watchLoading = ref<number | null>(null)
const watchError = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    lecture.value = await getLecture(String(route.params.sessionId))
  } catch (caught) {
    error.value = errorMessage(caught, 'The lecture details could not be loaded.')
  } finally {
    loading.value = false
  }
}

async function watchMoment(index: number) {
  if (!lecture.value?.recording_available) return
  watchLoading.value = index
  watchError.value = ''
  try {
    const url = await getWatchUrl(lecture.value.session_key, index)
    openSafely(url)
  } catch (caught) {
    watchError.value = errorMessage(caught, 'This positive moment could not be opened.')
  } finally {
    watchLoading.value = null
  }
}

function dialogueText(line: DialogueLine | string) {
  if (typeof line === 'string') return { speaker: '', text: line, start: '' }
  return {
    speaker: line.speaker ?? '',
    text: line.text ?? line.quote ?? '',
    start: line.start ?? '',
  }
}

function transcriptLines(clip: PositiveClip) {
  const lines = clip.dialogue
    .map(dialogueText)
    .filter((line) => line.text.trim())

  if (lines.length) return lines

  return [{
    speaker: clip.speaker ?? '',
    text: quote(clip),
    start: clip.start ?? '',
  }]
}

function displayTimestamp(value: string) {
  return value ? value.replace(/\.\d+$/, '') : 'Time unavailable'
}

function quote(clip: PositiveClip) {
  return clip.positive_quote || clip.quote || 'Positive learner feedback'
}

function formatClipDuration(value: number) {
  if (!Number.isFinite(value)) return ''
  if (value < 60) return `${Math.round(value)} sec`
  const minutes = Math.floor(value / 60)
  const seconds = Math.round(value % 60)
  return `${minutes}m ${seconds}s`
}

onMounted(load)
</script>

<template>
  <div class="w-full px-4 py-8 sm:px-6 lg:px-8 lg:py-10">
    <RouterLink to="/lectures" class="inline-flex items-center gap-2 text-sm font-semibold text-brand-700 hover:text-brand-800">
      <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2"><path d="m15 18-6-6 6-6"/></svg>
      Back to lectures
    </RouterLink>

    <LoadingSkeleton v-if="loading" class="mt-6" :rows="3" />
    <ErrorState v-else-if="error" class="mt-6" :message="error" @retry="load" />

    <template v-else-if="lecture">
      <section class="surface relative mt-6 overflow-hidden p-6 sm:p-8">
        <div class="absolute right-0 top-0 h-40 w-40 translate-x-1/3 -translate-y-1/3 rounded-full bg-brand-100/70" />
        <div class="relative">
          <div class="flex flex-col justify-between gap-6 sm:flex-row sm:items-start">
            <div class="max-w-2xl">
              <div class="flex flex-wrap items-center gap-2">
                <StatusBadge :value="lecture.clips_status" />
                <span class="badge bg-brand-50 text-brand-700">{{ lecture.positive_clips_count || 0 }} positive moments</span>
                <span class="badge bg-emerald-50 text-emerald-700">{{ lecture.ready_clips_count }} of {{ lecture.clips.length }} clips ready</span>
              </div>
              <h1 class="mt-4 text-2xl font-bold tracking-tight sm:text-3xl">{{ lecture.subject || 'Untitled lecture' }}</h1>
              <div class="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-sm text-muted">
                <span>{{ lecture.trainer || 'Trainer not assigned' }}</span>
                <span>{{ formatDate(lecture.date) }}</span>
              </div>
            </div>
            <button
              type="button"
              class="btn-primary shrink-0"
              :disabled="!lecture.recording_available"
              @click="lecture.recording_url && openSafely(lecture.recording_url)"
            >
              <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2">
                <path d="m9 7 8 5-8 5V7Z"/><circle cx="12" cy="12" r="9"/>
              </svg>
              {{ lecture.recording_available ? 'Watch full lecture' : 'Recording link pending' }}
            </button>
          </div>
        </div>
      </section>

      <div class="mt-8 flex items-end justify-between">
        <div>
          <p class="text-sm font-semibold text-brand-700">Analysis results</p>
          <h2 class="mt-1 text-2xl font-bold">Positive moments</h2>
        </div>
        <span class="text-sm text-muted">{{ lecture.clips.length }} moment{{ lecture.clips.length === 1 ? '' : 's' }}</span>
      </div>

      <p v-if="watchError" class="mt-4 rounded-xl bg-rose-50 px-4 py-3 text-sm text-rose-700" role="alert">{{ watchError }}</p>

      <div v-if="lecture.clips.length" class="mt-5 space-y-5">
        <article v-for="(clip, index) in lecture.clips" :key="`${clip.start}-${index}`" class="surface overflow-hidden">
          <div class="border-l-4 border-brand-500 p-5 sm:p-7">
            <div>
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2 text-xs font-semibold">
                  <span class="rounded-lg bg-slate-100 px-2.5 py-1.5 text-slate-600">{{ clip.start || 'Start unavailable' }} — {{ clip.end || 'End unavailable' }}</span>
                  <span v-if="clip.category" class="rounded-lg bg-brand-50 px-2.5 py-1.5 text-brand-700">{{ formatStatus(clip.category) }}</span>
                  <span v-if="clip.feedback_target" class="rounded-lg bg-cyan-50 px-2.5 py-1.5 text-cyan-700">{{ formatStatus(clip.feedback_target) }}</span>
                  <span v-if="clip.clip_asset" class="rounded-lg bg-emerald-50 px-2.5 py-1.5 text-emerald-700">Clip ready</span>
                  <span v-else class="rounded-lg bg-amber-50 px-2.5 py-1.5 text-amber-700">Clip pending</span>
                </div>
                <div v-if="clip.clip_asset" class="mt-4 rounded-xl border border-emerald-100 bg-emerald-50/60 px-4 py-3 text-sm">
                  <p class="font-semibold text-emerald-900">Positive clip {{ clip.clip_asset.clip_index }}</p>
                  <p class="mt-1 text-xs text-emerald-700">
                    <span v-if="clip.clip_asset.duration_seconds">{{ formatClipDuration(clip.clip_asset.duration_seconds) }}</span>
                    <span v-if="clip.clip_asset.duration_seconds && clip.clip_asset.uploaded_at"> · </span>
                    <span v-if="clip.clip_asset.uploaded_at">Uploaded {{ formatDate(clip.clip_asset.uploaded_at) }}</span>
                  </p>
                </div>
                <div class="mt-4 flex w-full max-w-xl flex-col gap-2 sm:flex-row">
                  <button
                    type="button"
                    class="btn-primary w-full sm:flex-1"
                    :disabled="!lecture.recording_available || watchLoading === index"
                    :title="lecture.recording_available ? 'Open full lecture at this timestamp' : 'Recording link pending'"
                    @click="watchMoment(index)"
                  >
                    <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2">
                      <path d="m9 7 8 5-8 5V7Z"/><circle cx="12" cy="12" r="9"/>
                    </svg>
                    {{ !lecture.recording_available ? 'Recording link pending' : watchLoading === index ? 'Opening…' : 'Watch in full lecture' }}
                  </button>
                  <a
                    v-if="clip.clip_asset"
                    class="btn-secondary w-full sm:flex-1"
                    :href="clip.clip_asset.url"
                    target="_blank"
                    rel="noopener noreferrer"
                    title="Open the uploaded trimmed clip"
                  >
                    <svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2">
                      <path d="m9 7 8 5-8 5V7Z"/><circle cx="12" cy="12" r="9"/>
                    </svg>
                    Watch clip
                  </a>
                </div>
                <div class="mt-5 space-y-3" role="list" aria-label="Timestamped transcript">
                  <div
                    v-for="(line, lineIndex) in transcriptLines(clip)"
                    :key="`${line.start}-${line.speaker}-${lineIndex}`"
                    class="grid gap-2 rounded-xl bg-slate-50 px-4 py-3 sm:grid-cols-[6.5rem_1fr] sm:gap-4"
                    role="listitem"
                  >
                    <time class="text-xs font-semibold tabular-nums text-brand-700">
                      {{ displayTimestamp(line.start) }}
                    </time>
                    <div class="min-w-0">
                      <p v-if="line.speaker" class="text-sm font-bold text-slate-800">{{ line.speaker }}</p>
                      <p class="mt-0.5 text-base leading-7 text-ink">{{ line.text }}</p>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <div v-if="clip.reason || typeof clip.confidence === 'number' || clip.semantic_verification.verdict" class="mt-6 grid gap-3 sm:grid-cols-3">
              <div v-if="typeof clip.confidence === 'number'" class="rounded-xl bg-slate-50 p-4">
                <p class="text-xs font-semibold uppercase tracking-wide text-muted">Confidence</p>
                <p class="mt-1 font-bold">{{ formatConfidence(clip.confidence) }}</p>
              </div>
              <div v-if="clip.semantic_verification.verdict" class="rounded-xl bg-slate-50 p-4">
                <p class="text-xs font-semibold uppercase tracking-wide text-muted">Semantic check</p>
                <div class="mt-1"><StatusBadge :value="clip.semantic_verification.verdict" /></div>
              </div>
              <div v-if="typeof clip.semantic_verification.confidence === 'number'" class="rounded-xl bg-slate-50 p-4">
                <p class="text-xs font-semibold uppercase tracking-wide text-muted">Verification confidence</p>
                <p class="mt-1 font-bold">{{ formatConfidence(clip.semantic_verification.confidence) }}</p>
              </div>
            </div>

            <div v-if="clip.reason || clip.semantic_verification.reason" class="mt-5 space-y-3 text-sm leading-6 text-muted">
              <p v-if="clip.reason"><span class="font-semibold text-slate-700">Classification:</span> {{ clip.reason }}</p>
              <p v-if="clip.semantic_verification.reason"><span class="font-semibold text-slate-700">Verification:</span> {{ clip.semantic_verification.reason }}</p>
            </div>
          </div>
        </article>
      </div>

      <div v-else class="surface mt-5 px-6 py-14 text-center">
        <span class="mx-auto grid h-12 w-12 place-items-center rounded-full bg-brand-50 text-brand-700">
          <svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M7 8h10M7 12h6m-7 8 3.5-3H18a3 3 0 0 0 3-3V6a3 3 0 0 0-3-3H6a3 3 0 0 0-3 3v8a3 3 0 0 0 3 3v3Z"/>
          </svg>
        </span>
        <h3 class="mt-4 font-bold">No positive moments found</h3>
        <p class="mt-1 text-sm text-muted">This lecture was analyzed successfully but did not contain a qualifying positive mention.</p>
      </div>
    </template>
  </div>
</template>
