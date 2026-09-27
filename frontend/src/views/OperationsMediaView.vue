<script setup lang="ts">
/**
 * Operations → Positive Moments Media.
 *
 * The operator's whole workflow, without a terminal:
 *
 *   choose a range → Preview → Start analysis → review moments and the cost
 *   estimate → Render all safe clips → watch clips arrive in SharePoint
 *
 * Every button queues a durable run for the background media runner and
 * returns at once; the page polls lightweight persisted status. Closing the
 * tab stops nothing. Analysis never renders: spending starts only with an
 * explicit Render.
 *
 * Every counter is a filter. Clicking one shows exactly the lectures behind
 * the number, using the filter keys the platform attached to each row.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppIcon from '../components/AppIcon.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import LoadingSkeleton from '../components/LoadingSkeleton.vue'
import PageHeader from '../components/PageHeader.vue'
import SectionPanel from '../components/SectionPanel.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { getMediaDashboard, openRecording, queueMediaRun } from '../services/media'
import type { MediaDashboard, MediaLectureRow, RunMode } from '../types/media'
import { businessDate, cairoDateTime, sinceNow } from '../utils/datetime'
import { errorMessage, openSafely } from '../utils/format'
import {
  RUN_MODE, analysisState, humanise, mediaRecordingState, mediaStatus, transcriptState,
} from '../utils/labels'

const route = useRoute()
const router = useRouter()

function cairoToday(): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Africa/Cairo' }).format(new Date())
}
function shift(day: string, days: number): string {
  const date = new Date(`${day}T12:00:00Z`)
  date.setUTCDate(date.getUTCDate() + days)
  return date.toISOString().slice(0, 10)
}

/** A useful recent range by default: the last 14 days. Nothing is tied to a month. */
const from = ref(typeof route.query.from === 'string' ? route.query.from : shift(cairoToday(), -13))
const to = ref(typeof route.query.to === 'string' ? route.query.to : cairoToday())
const filter = ref(typeof route.query.filter === 'string' ? route.query.filter : 'all')

const board = ref<MediaDashboard | null>(null)
const loading = ref(true)
const error = ref('')
const notice = ref('')
const busy = ref(false)
const confirmRender = ref(false)
let poll: ReturnType<typeof setInterval> | undefined

const rangeProblem = computed(() => {
  if (!from.value || !to.value) return 'Choose both dates.'
  if (to.value < from.value) return 'The end date is before the start date.'
  const days = (Date.parse(to.value) - Date.parse(from.value)) / 86_400_000 + 1
  if (days > 62) return 'Choose at most 62 days.'
  return ''
})

const rows = computed<MediaLectureRow[]>(() =>
  (board.value?.lectures ?? []).filter(row => row.filters.includes(filter.value)))
const activeCounter = computed(() => board.value?.counters.find(c => c.filter === filter.value))
const activeRuns = computed(() => board.value?.active_runs ?? [])
const cost = computed(() => board.value?.cost_preview ?? null)
const readiness = computed(() => board.value?.readiness ?? null)

async function load(quiet = false) {
  if (rangeProblem.value) return
  if (!quiet) loading.value = true
  try {
    board.value = await getMediaDashboard(from.value, to.value)
    error.value = ''
  } catch (caught) {
    if (!quiet) error.value = errorMessage(caught, 'Positive Moments Media could not be loaded.')
  } finally {
    loading.value = false
  }
}

function syncQuery() {
  router.replace({ query: { ...route.query, from: from.value, to: to.value,
                            filter: filter.value === 'all' ? undefined : filter.value } })
}

onMounted(() => {
  load()
  poll = setInterval(() => { if (activeRuns.value.length || inProgress.value) load(true) }, 8000)
})
onBeforeUnmount(() => { if (poll) clearInterval(poll) })
watch([from, to], () => { syncQuery(); load() })
watch(filter, syncQuery)

const inProgress = computed(() => (board.value?.lectures ?? []).some(row =>
  row.filters.includes('rendering') || row.filters.includes('uploading')))

async function queue(mode: RunMode) {
  busy.value = true
  notice.value = ''
  try {
    await queueMediaRun(mode, from.value, to.value)
    notice.value = `${RUN_MODE[mode]} queued for ${businessDate(from.value)} – ${businessDate(to.value)}. `
      + 'It runs in the background; this page updates by itself.'
    confirmRender.value = false
    await load(true)
  } catch (caught) {
    notice.value = errorMessage(caught, 'The request was refused. Nothing was queued.')
  } finally {
    busy.value = false
  }
}

async function openFull(lectureId: string) {
  try { openSafely(await openRecording(lectureId)) } catch (caught) {
    notice.value = errorMessage(caught, 'This lecture has no recording that can be opened yet.')
  }
}

function progress(run: { total_lectures: number; processed_lectures: number }) {
  return run.total_lectures ? Math.round((run.processed_lectures / run.total_lectures) * 100) : 0
}
</script>

<template>
  <div class="page stack">
    <RouterLink class="btn-quiet" :to="{ name: 'operations' }">
      <AppIcon name="chevronLeft" :size="14" /> Operations
    </RouterLink>

    <PageHeader kicker="Operations" title="Positive Moments Media">
      <template #meta>
        <p class="page-lede mt-2 max-w-3xl">
          Find learner-positive evidence in stored transcripts, cut each moment from the lecture's
          resolved recording with a minute of context either side, and deliver the clips to SharePoint.
          Analysis never spends render credits; rendering starts only when you ask.
        </p>
      </template>
    </PageHeader>

    <SectionPanel title="Range and actions">
      <div class="flex flex-wrap items-end gap-3">
        <label class="filter-field">
          <span class="filter-label">From</span>
          <input v-model="from" type="date" class="field field-sm">
        </label>
        <label class="filter-field">
          <span class="filter-label">To</span>
          <input v-model="to" type="date" class="field field-sm">
        </label>
        <div class="flex flex-wrap gap-2">
          <button type="button" class="btn-secondary" :disabled="busy || !!rangeProblem" @click="queue('PREVIEW')">
            <AppIcon name="search" :size="16" /> Preview
          </button>
          <button type="button" class="btn-primary" :disabled="busy || !!rangeProblem" @click="queue('ANALYZE')">
            <AppIcon name="sparkle" :size="16" /> Start analysis
          </button>
          <button
            type="button" class="btn-primary"
            :disabled="busy || !!rangeProblem || !cost?.safe_clips_planned"
            @click="confirmRender = true"
          >
            <AppIcon name="video" :size="16" /> Render all safe clips
          </button>
          <button type="button" class="btn-quiet" :disabled="busy || !!rangeProblem" @click="queue('RETRY_FAILED')">
            <AppIcon name="refresh" :size="16" /> Retry failed
          </button>
        </div>
      </div>
      <p v-if="rangeProblem" class="notice-error mt-3">{{ rangeProblem }}</p>
      <p v-if="notice" class="notice mt-3" role="status"><AppIcon name="info" :size="16" class="mt-0.5" />{{ notice }}</p>

      <div v-if="confirmRender && cost" class="notice-warn mt-3" role="alertdialog" aria-label="Confirm rendering">
        <AppIcon name="info" :size="16" class="mt-0.5" />
        <div class="min-w-0">
          <p>
            Render <strong>{{ cost.safe_clips_planned }}</strong> safe clip{{ cost.safe_clips_planned === 1 ? '' : 's' }}
            (~{{ cost.planned_output_minutes }} output minutes, <strong>estimated</strong>
            {{ cost.estimated_credits }} Creatomate credits). Clips already delivered are never rendered again.
          </p>
          <div class="mt-2 flex flex-wrap gap-2">
            <button type="button" class="btn-primary btn-sm" :disabled="busy" @click="queue('RENDER')">Yes, render</button>
            <button type="button" class="btn-quiet btn-sm" @click="confirmRender = false">Not now</button>
          </div>
        </div>
      </div>

      <div v-if="readiness && (!readiness.render_provider_configured || !readiness.sharepoint_destination_configured)" class="notice-warn mt-3" role="note">
        <AppIcon name="alert" :size="16" class="mt-0.5" />
        <span>
          <template v-if="!readiness.render_provider_configured">Rendering is not configured on the server yet (Creatomate key missing). </template>
          <template v-if="!readiness.sharepoint_destination_configured">The SharePoint destination is not configured. </template>
          Preview and analysis still work.
        </span>
      </div>
    </SectionPanel>

    <SectionPanel v-if="activeRuns.length" title="Working in the background" :count="activeRuns.length">
      <div v-for="run in activeRuns" :key="run.run_id" class="mb-3 last:mb-0">
        <div class="flex flex-wrap items-center justify-between gap-2 text-sm">
          <span>
            <strong>{{ RUN_MODE[run.mode] ?? run.mode }}</strong>
            · {{ businessDate(run.requested_from) }} – {{ businessDate(run.requested_to) }}
            · {{ run.processed_lectures }} of {{ run.total_lectures || '…' }} lectures
          </span>
          <span class="text-muted">{{ run.started_at ? `started ${sinceNow(run.started_at)}` : 'waiting for the media runner' }}</span>
        </div>
        <div
          class="mt-2 h-2 w-full overflow-hidden rounded-full bg-line" role="progressbar"
          :aria-valuenow="progress(run)" aria-valuemin="0" aria-valuemax="100"
        >
          <div class="h-full rounded-full bg-brand-600 transition-all" :style="{ width: `${progress(run)}%` }" />
        </div>
      </div>
    </SectionPanel>

    <LoadingSkeleton v-if="loading" variant="cards" :rows="4" />
    <ErrorState v-else-if="error" :message="error" @retry="load()" />

    <template v-else-if="board">
      <section class="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5" aria-label="Counters">
        <button
          v-for="counter in board.counters"
          :key="counter.filter"
          type="button"
          class="tile text-left transition hover:ring-2 hover:ring-brand-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
          :class="filter === counter.filter ? 'ring-2 ring-brand-600' : ''"
          :aria-pressed="filter === counter.filter"
          @click="filter = counter.filter"
        >
          <p class="tile-label">{{ counter.label }}</p>
          <p class="tile-value" :class="counter.count === 0 && counter.filter !== 'all' ? '!text-faint' : 'text-ink'">
            {{ counter.count }}
          </p>
        </button>
      </section>

      <SectionPanel v-if="cost" title="Render estimate" note="An estimate only, from the provider's published pixel rule. Nothing is rendered until you press Render.">
        <dl class="grid gap-3 text-sm sm:grid-cols-3">
          <div><dt class="tile-label">Safe clips planned</dt><dd class="tile-value text-ink">{{ cost.safe_clips_planned }}</dd></div>
          <div><dt class="tile-label">Output minutes</dt><dd class="tile-value text-ink">{{ cost.planned_output_minutes }}</dd></div>
          <div><dt class="tile-label">Estimated credits</dt><dd class="tile-value text-ink">≈ {{ cost.estimated_credits }}</dd></div>
        </dl>
        <p class="mt-2 text-2xs text-faint">
          {{ cost.render_policy.max_width }}×{{ cost.render_policy.max_height }} at {{ cost.render_policy.frame_rate }} fps
          ≈ {{ cost.credits_per_output_minute }} credits per output minute.
        </p>
      </SectionPanel>

      <SectionPanel :title="activeCounter?.label ?? 'Lectures'" :count="rows.length" flush>
        <template #actions>
          <button v-if="filter !== 'all'" type="button" class="btn-quiet btn-sm" @click="filter = 'all'">
            <AppIcon name="close" :size="14" /> Show all
          </button>
        </template>
        <EmptyState
          v-if="!rows.length" compact tone="neutral" icon="inbox"
          :title="board.lectures.length ? 'No lecture matches this counter' : 'No lectures in this range'"
          :message="board.lectures.length ? 'Choose another counter above.' : 'Choose a different date range.'"
        />
        <div v-else class="table-wrap">
          <table class="data-table">
            <thead>
              <tr>
                <th scope="col">Date</th>
                <th scope="col">Lecture</th>
                <th scope="col">Transcript</th>
                <th scope="col">Recording</th>
                <th scope="col">Analysis</th>
                <th scope="col" class="text-right">Moments</th>
                <th scope="col">Media</th>
                <th scope="col" class="text-right">Clips</th>
                <th scope="col">Updated</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in rows" :key="row.lecture_id">
                <td class="whitespace-nowrap">{{ businessDate(row.session_date) }}</td>
                <td class="min-w-[12rem]">
                  <RouterLink
                    class="row-link font-semibold"
                    :to="{ name: 'lecture', params: { lectureId: row.lecture_id }, query: { tab: 'moments' } }"
                  >
                    {{ row.subject ?? 'Lecture' }}
                  </RouterLink>
                  <p v-if="row.trainer" class="text-2xs text-muted">{{ row.trainer }}</p>
                </td>
                <td><StatusBadge v-bind="transcriptState(row.transcript_state)" /></td>
                <td>
                  <button
                    v-if="row.recording_state === 'READY'"
                    type="button" class="btn-quiet btn-sm !px-1.5" title="Open full recording"
                    @click="openFull(row.lecture_id)"
                  >
                    <AppIcon name="play" :size="14" /> Open
                  </button>
                  <RouterLink
                    v-else
                    :to="{ name: 'lecture', params: { lectureId: row.lecture_id }, query: { tab: 'moments' } }"
                    :title="row.recording_reason ?? undefined"
                  >
                    <StatusBadge v-bind="mediaRecordingState(row.recording_state)" :title="row.recording_status ?? undefined" />
                    <p v-if="row.recording_status" class="mt-0.5 text-2xs text-muted">{{ humanise(row.recording_status) }}</p>
                  </RouterLink>
                </td>
                <td><StatusBadge v-bind="analysisState(row.analysis_state)" /></td>
                <td class="text-right tabular-nums">{{ row.moment_count ?? '—' }}</td>
                <td>
                  <StatusBadge v-bind="mediaStatus(row.media_state)" />
                  <p v-if="row.review_items" class="mt-0.5 text-2xs text-orange-700">{{ row.review_items }} need review</p>
                </td>
                <td class="text-right tabular-nums">{{ row.completed_clips }}</td>
                <td class="whitespace-nowrap text-muted">{{ row.refreshed_at ? cairoDateTime(row.refreshed_at) : 'Not previewed' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </SectionPanel>

      <p class="text-2xs text-faint">Analysis policy {{ board.analysis_policy_version }}</p>
    </template>
  </div>
</template>
