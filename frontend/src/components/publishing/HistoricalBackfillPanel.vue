<script setup lang="ts">
/**
 * Historical Backfill (inside the Lecture Publishing Monitor).
 *
 *   From / To (inclusive Cairo business dates) -> Preview Backfill: the publisher service discovers the range
 *   LIVE in Microsoft Teams, read-only (nothing is registered, queued or imported), and compares it with what
 *   the publisher already holds -> Run Backfill (operators only, after an explicit confirmation, and only for a
 *   COMPLETE preview) -> the service registers the Teams lectures and works through them behind daily publishing.
 *
 * Nothing on this page publishes anything itself. Every state is re-read from the server, so a preview or a
 * run survives a page refresh or a service restart, and the browser does not need to stay open.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppIcon from '../AppIcon.vue'
import EmptyState from '../EmptyState.vue'
import SectionPanel from '../SectionPanel.vue'
import StatusBadge from '../StatusBadge.vue'
import {
  cancelBackfill, getBackfillPreview, getBackfillRun, getBackfillRuns, previewBackfill, retryBackfill, startBackfill,
} from '../../services/publishing'
import type { BackfillPreview, BackfillRunDetail, BackfillRuns, DayCoverage } from '../../types/backfill'
import { CATEGORY, COUNTERS, QA_ONLY_CODE, VERIFICATION, backfillLabel, canRun, duration, provenance, rangeProblem, rowKey } from '../../utils/backfill'
import { businessDate, cairoDateTime, cairoTimeRange, sinceNow } from '../../utils/datetime'
import { errorMessage } from '../../utils/format'
import { label } from '../../utils/publishing'

const POLL_MS = 15_000
const PREVIEW_POLL_MS = 3_000

const route = useRoute()
const router = useRouter()
const query = (name: string) => (typeof route.query[name] === 'string' ? (route.query[name] as string) : '')

const runs = ref<BackfillRuns | null>(null)
const runsError = ref('')
const today = computed(() => runs.value?.today ?? new Intl.DateTimeFormat('en-CA', { timeZone: 'Africa/Cairo' }).format(new Date()))
const maxDays = computed(() => runs.value?.max_days ?? 31)
const dateFrom = ref(query('bf_from'))
const dateTo = ref(query('bf_to'))
const problem = computed(() => rangeProblem(dateFrom.value, dateTo.value, today.value, maxDays.value))

const preview = ref<BackfillPreview | null>(null)
const previewing = ref(false)
const previewError = ref('')
const categoryFilter = ref('')
const previewDone = computed(() => Boolean(preview.value?.preview.is_finished))
const previewRows = computed(() => (preview.value?.lectures ?? [])
  .filter(r => !categoryFilter.value || r.category === categoryFilter.value))
const runnable = computed(() => canRun(preview.value, dateFrom.value, dateTo.value, Boolean(runs.value?.operator),
  Boolean(runs.value?.active)))
const runBlockedBecause = computed(() => {
  const p = preview.value
  if (!p) return 'Preview the range first.'
  if (p.preview.date_from !== dateFrom.value || p.preview.date_to !== dateTo.value) return 'The dates changed: preview again.'
  if (!p.preview.is_finished) return 'The preview is still discovering the range in Teams.'
  if (!p.complete) return 'The preview is INCOMPLETE: not every day could be discovered in Teams. Preview again.'
  if (!p.confirmable) return 'The preview is older than 30 minutes: preview again.'
  if (!runs.value?.operator) return 'Starting a backfill needs the publisher backfill operator role.'
  if (runs.value?.active) return 'Another historical backfill is running.'
  if (!(p.summary?.eligible)) return 'No lecture in this range needs processing.'
  return ''
})

const confirming = ref(false)
const confirmReason = ref('')
const starting = ref(false)
const startError = ref('')

const selectedRun = ref<string | null>(query('run') || null)
const detail = ref<BackfillRunDetail | null>(null)
const detailError = ref('')
const itemFilter = ref('')
const detailRows = computed(() => (detail.value?.lectures ?? []).filter(l => !itemFilter.value || l.status === itemFilter.value))
const acting = ref(false)
const actionMessage = ref('')
const retryReason = ref('')
let timer: ReturnType<typeof setInterval> | undefined
let previewTimer: ReturnType<typeof setTimeout> | undefined

async function loadRuns() {
  try {
    runs.value = await getBackfillRuns()
    runsError.value = ''
  } catch (caught) {
    runsError.value = errorMessage(caught, 'The historical backfill runs could not be read.')
  }
}

async function loadDetail() {
  if (!selectedRun.value) { detail.value = null; return }
  try {
    detail.value = await getBackfillRun(selectedRun.value)
    detailError.value = ''
  } catch (caught) {
    detailError.value = errorMessage(caught, 'This run could not be read.')
  }
}

/** Follow a preview until the publisher service has discovered every day of it. */
async function followPreview(previewId: string) {
  if (previewTimer) clearTimeout(previewTimer)
  try {
    preview.value = await getBackfillPreview(previewId)
    previewError.value = ''
  } catch (caught) {
    previewError.value = errorMessage(caught, 'The preview could not be read.')
  }
  if (preview.value && !preview.value.preview.is_finished) {
    previewTimer = setTimeout(() => followPreview(previewId), PREVIEW_POLL_MS)
  } else {
    previewing.value = false
  }
}

async function runPreview() {
  if (problem.value) return
  previewing.value = true
  previewError.value = ''
  startError.value = ''
  categoryFilter.value = ''
  try {
    const answer = await previewBackfill(dateFrom.value, dateTo.value)
    preview.value = answer
    syncQuery()
    await followPreview(answer.preview.run_id)
  } catch (caught) {
    preview.value = null
    previewing.value = false
    previewError.value = errorMessage(caught, 'The preview could not be requested.')
  }
}

async function confirmRun() {
  if (!preview.value) return
  starting.value = true
  startError.value = ''
  try {
    const answer = await startBackfill(preview.value, confirmReason.value.trim())
    confirming.value = false
    confirmReason.value = ''
    preview.value = null
    selectedRun.value = answer.run.run_id
    await Promise.all([loadRuns(), loadDetail()])
  } catch (caught) {
    startError.value = errorMessage(caught, 'The backfill could not be started.')
  } finally {
    starting.value = false
  }
}

async function cancelRun() {
  if (!detail.value || !window.confirm('Cancel the pending work of this run? Imports already sent to Bunny finish normally.')) return
  acting.value = true
  try {
    actionMessage.value = (await cancelBackfill(detail.value.run.run_id)).detail
    await Promise.all([loadRuns(), loadDetail()])
  } catch (caught) {
    actionMessage.value = errorMessage(caught, 'The cancellation could not be requested.')
  } finally {
    acting.value = false
  }
}

async function retryRun() {
  if (!detail.value || !retryReason.value.trim()) return
  acting.value = true
  try {
    const answer = await retryBackfill(detail.value.run.run_id, retryReason.value.trim())
    actionMessage.value = `${answer.lectures_reopened} lecture(s) re-opened, ${answer.jobs_reopened} publisher job(s) retried`
      + (answer.lms_rematch ? '; the LMS match is checked again' : '')
      + (answer.refused.length ? `; ${answer.refused.length} job(s) refused (${[...new Set(answer.refused.map(r => r.code))].join(', ')})` : '')
      + '.'
    retryReason.value = ''
    await Promise.all([loadRuns(), loadDetail()])
  } catch (caught) {
    actionMessage.value = errorMessage(caught, 'The retry could not be requested.')
  } finally {
    acting.value = false
  }
}

function syncQuery() {
  router.replace({ query: { ...route.query, bf_from: dateFrom.value || undefined, bf_to: dateTo.value || undefined,
    run: selectedRun.value ?? undefined, preview: preview.value?.preview.run_id ?? undefined } })
}

watch([dateFrom, dateTo], syncQuery)
watch(selectedRun, () => { actionMessage.value = ''; syncQuery(); loadDetail() })

onMounted(async () => {
  await loadRuns()
  if (!dateFrom.value) dateFrom.value = today.value
  if (!dateTo.value) dateTo.value = dateFrom.value
  if (!selectedRun.value && runs.value?.active) selectedRun.value = runs.value.active.run_id
  // A preview survives a refresh: the one named in the URL, or the one still being discovered.
  const latest = runs.value?.latest_preview
  const resume = query('preview') || (latest && !latest.is_finished ? latest.run_id : '')
  if (resume) {
    previewing.value = true
    await followPreview(resume)
    if (preview.value) {
      dateFrom.value = preview.value.preview.date_from
      dateTo.value = preview.value.preview.date_to
    }
  }
  await loadDetail()
  timer = setInterval(() => {
    if (document.visibilityState !== 'visible') return
    loadRuns()
    if (selectedRun.value && !detail.value?.run.is_finished) loadDetail()
  }, POLL_MS)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  if (previewTimer) clearTimeout(previewTimer)
})

/** One day's chip: each source's answer, and the day's count (an empty day only when both sources answered). */
function dayTitle(day: DayCoverage): string {
  const error = day.error ? ` · ${day.error}` : ''
  return `Teams: ${day.teams?.status ?? day.status} · QA database: ${day.qa?.status ?? 'NOT_CHECKED'}${error}`
}
function daySources(day: DayCoverage): string {
  const teams = backfillLabel.coverage(day.teams?.status ?? day.status).label.toLowerCase()
  const qa = day.qa?.status === 'COMPLETE' ? `${day.qa.records ?? 0} QA` : `QA ${backfillLabel.coverage(day.qa?.status).label.toLowerCase()}`
  return `Teams ${teams}, ${qa}`
}
function dayCount(date: string): string {
  const day = preview.value?.summary?.per_day[date]
  if (day?.outcome === 'COMPLETED_EMPTY_DAY') return 'empty day'
  return `${day?.eligible ?? 0}/${day?.total ?? 0}`
}

const summaryTiles = computed(() => {
  const s = preview.value?.summary
  if (!s) return []
  return [
    { label: 'Unique lectures', value: String(s.total) },
    { label: 'Teams lectures discovered', value: String(s.teams_discovered) },
    { label: 'QA database records', value: String(s.qa_records ?? 0) },
    { label: 'QA records matched', value: String(s.qa_matched ?? 0) },
    { label: 'QA database only', value: String(s.qa_only ?? 0), tone: s.qa_only ? 'text-orange-700' : '' },
    { label: 'Existing lectures', value: String(s.existing) },
    { label: 'Suppressed duplicates', value: String(s.suppressed ?? 0) },
    { label: 'Newly discovered', value: String(s.new), tone: s.new ? 'text-brand-700' : '' },
    { label: 'Already published', value: String(s.already_published) },
    { label: 'Ready to process', value: String(s.ready), tone: s.ready ? 'text-brand-700' : '' },
    { label: 'Incomplete · resume', value: String(s.resume), tone: s.resume ? 'text-brand-700' : '' },
    { label: 'Already processing', value: String(s.already_processing) },
    { label: 'Missing LMS match', value: String(s.missing_lms), tone: s.missing_lms ? 'text-orange-700' : '' },
    { label: 'Missing recording', value: String(s.missing_recording), tone: s.missing_recording ? 'text-orange-700' : '' },
    { label: 'Needs review', value: String(s.review), tone: s.review ? 'text-orange-700' : '' },
    { label: 'Est. new Bunny imports', value: s.estimated_imports.possible
      ? `${s.estimated_imports.known}–${s.estimated_imports.upper_bound}` : String(s.estimated_imports.known) },
    { label: 'Known selected duration', value: duration(s.known_selected_duration_seconds) },
  ]
})
const shortId = (id: string) => id.slice(0, 8)
const verified = (code?: string) => VERIFICATION[code ?? ''] ?? code ?? ''
</script>

<template>
  <div class="stack">
    <!-- Range and actions -->
    <section class="surface px-4 py-4" aria-label="Historical backfill range">
      <div class="flex flex-wrap items-end gap-3">
        <label class="filter-field">
          <span class="filter-label">From Date</span>
          <input v-model="dateFrom" type="date" class="field field-sm" :max="today" data-test="bf-from">
        </label>
        <label class="filter-field">
          <span class="filter-label">To Date</span>
          <input v-model="dateTo" type="date" class="field field-sm" :min="dateFrom" :max="today" data-test="bf-to">
        </label>
        <button type="button" class="btn-secondary" :disabled="Boolean(problem) || previewing" data-test="bf-preview" @click="runPreview">
          <AppIcon name="search" :size="16" /> {{ previewing ? 'Discovering…' : 'Preview Backfill' }}
        </button>
        <button
          type="button" class="btn-primary" :disabled="!runnable" :title="runBlockedBecause" data-test="bf-run"
          @click="confirming = true; startError = ''"
        >
          <AppIcon name="refresh" :size="16" /> Run Backfill
        </button>
      </div>
      <p class="mt-2 text-xs text-muted">
        Both dates are included. Dates are Cairo business dates (the publisher's lecture date); at most {{ maxDays }} days per run.
        Preview discovers the range live in Microsoft Teams and compares it with the publisher's records — read-only: nothing is
        registered, queued, imported or released.
        <template v-if="runs && !runs.operator"> You can preview; starting a run needs the backfill operator role.</template>
      </p>
      <p v-if="problem && (dateFrom || dateTo)" class="notice-error mt-2" data-test="bf-problem">{{ problem }}</p>
      <p v-if="previewError" class="notice-error mt-2">{{ previewError }}</p>
      <p v-if="!runnable && preview && runBlockedBecause" class="mt-2 text-xs text-muted" data-test="bf-blocked">{{ runBlockedBecause }}</p>
    </section>

    <!-- Preview progress and coverage -->
    <section v-if="preview" class="surface px-4 py-3" aria-label="Teams and QA discovery coverage" data-test="bf-coverage">
      <div class="flex flex-wrap items-center gap-2">
        <p class="tile-label">Teams + QA discovery</p>
        <StatusBadge v-bind="backfillLabel.run(preview.preview.status)" />
        <span class="text-xs text-muted">{{ preview.preview.days_discovered }} / {{ preview.preview.coverage.length }} days discovered</span>
      </div>
      <div class="mt-2 flex flex-wrap gap-1.5">
        <span
          v-for="day in preview.preview.coverage" :key="day.date" class="chip tabular-nums"
          :class="day.status === 'COMPLETE' ? '' : day.status === 'PENDING' ? 'opacity-60' : 'text-rose-700'"
          :title="dayTitle(day)"
          :data-test="`bf-day-${day.date}`"
        >
          {{ businessDate(day.date) }} · {{ backfillLabel.coverage(day.status).label }}<template v-if="day.qa"> · {{ daySources(day) }}</template><template v-if="day.status === 'COMPLETE' && preview.summary"> · {{ dayCount(day.date) }}</template>
        </span>
      </div>
      <p v-if="previewDone && !preview.complete" class="notice-error mt-2" role="alert" data-test="bf-incomplete">
        INCOMPLETE preview: {{ preview.summary?.incomplete_days?.length ?? 0 }} day(s) could not be discovered in both Teams and the QA database
        ({{ (preview.summary?.incomplete_days ?? []).map(d => businessDate(d)).join(', ') }}). The lists below are not the whole range and cannot be run.
      </p>
    </section>

    <!-- Preview -->
    <template v-if="preview && previewDone && preview.summary">
      <section class="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-6" aria-label="Preview summary">
        <div v-for="tile in summaryTiles" :key="tile.label" class="tile">
          <p class="tile-label">{{ tile.label }}</p>
          <p class="tile-value" :class="tile.tone">{{ tile.value }}</p>
        </div>
      </section>

      <SectionPanel
        title="Preview" :count="preview.summary.total" flush
        :note="`Teams checked live (read-only); LMS match checked as a dry run; recordings, imports and releases are verified only by the run. ${preview.complete ? 'Every day of the range was discovered.' : 'INCOMPLETE: some days are missing.'}`"
      >
        <div class="flex flex-wrap items-center gap-2 px-4 py-2">
          <label class="filter-field">
            <span class="filter-label">Show</span>
            <select v-model="categoryFilter" class="field field-sm w-auto">
              <option value="">Every lecture</option>
              <option v-for="(b, key) in CATEGORY" :key="key" :value="key">{{ b.label }} ({{ preview.summary.categories[key] ?? 0 }})</option>
            </select>
          </label>
        </div>
        <EmptyState v-if="!previewRows.length" compact tone="neutral" icon="inbox" title="No lecture" message="No lecture in this range matches." />
        <div v-else class="overflow-x-auto">
          <table class="data-table" data-test="bf-preview-table">
            <thead>
              <tr>
                <th scope="col">Lecture</th>
                <th scope="col">Date</th>
                <th scope="col">Origin</th>
                <th scope="col">LMS Match</th>
                <th scope="col">Recording</th>
                <th scope="col">Selected Duration</th>
                <th scope="col">Bunny Status</th>
                <th scope="col">Proposed Action</th>
                <th scope="col">Reason</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in previewRows" :key="rowKey(row)">
                <td class="min-w-[14rem]">
                  <p class="font-semibold text-ink">{{ row.subject ?? 'Lecture' }}</p>
                  <p class="text-2xs text-muted">{{ cairoTimeRange(row.scheduled_start, row.scheduled_end) }}<template v-if="row.module"> · {{ row.module }}</template></p>
                </td>
                <td class="whitespace-nowrap">{{ businessDate(row.session_date) }}</td>
                <td>
                  <StatusBadge v-bind="backfillLabel.origin(row.origin)" />
                  <p v-if="row.sources" class="text-2xs text-muted" data-test="bf-provenance">{{ provenance(row.sources) }}<template v-if="row.qa?.status === 'CONFLICT'"> · QA conflict</template><template v-else-if="(row.qa?.records ?? 0) > 1"> · {{ row.qa?.records }} QA records</template></p>
                </td>
                <td>
                  <StatusBadge v-bind="label.lms(row.lms.status)" />
                  <p class="text-2xs text-muted">{{ verified(row.verification?.lms) }}</p>
                </td>
                <td>
                  <StatusBadge v-bind="label.recording(row.recording.status)" :title="row.recording.reason" />
                  <p class="text-2xs text-muted">
                    <template v-if="row.verification?.recording === 'STORED'">{{ row.recording.verified_count }} verified<template v-if="row.recording.selected_part"> · part {{ row.recording.selected_part }} selected</template></template>
                    <template v-else>{{ verified(row.verification?.recording) }}</template>
                  </p>
                </td>
                <td class="whitespace-nowrap tabular-nums">{{ duration(row.recording.selected_duration_seconds) }}</td>
                <td>
                  <template v-if="row.bunny">
                    <StatusBadge v-bind="label.bunny(row.bunny.status)" />
                    <p class="text-2xs text-muted">{{ label.release(row.release).label }}<template v-if="row.playback?.available"> · playable</template></p>
                  </template>
                  <span v-else class="text-muted">—</span>
                </td>
                <td>
                  <StatusBadge v-bind="backfillLabel.category(row.category)" />
                  <p class="mt-0.5 text-2xs text-muted">{{ backfillLabel.action(row.proposed_action).label }}</p>
                </td>
                <td class="max-w-xs text-2xs leading-snug text-muted">
                  <p v-if="row.code && QA_ONLY_CODE[row.code]" class="font-semibold text-ink">{{ QA_ONLY_CODE[row.code] }}</p>
                  {{ row.reason }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </SectionPanel>
    </template>

    <!-- Runs history -->
    <SectionPanel title="Runs" :count="runs?.runs.length ?? 0" note="Newest first. Select a run for its lecture-by-lecture progress." flush>
      <p v-if="runsError" class="notice-error m-4">{{ runsError }}</p>
      <EmptyState v-else-if="!runs?.runs.length" compact tone="neutral" icon="inbox" title="No historical backfill yet" message="Preview a range, then run it." />
      <div v-else class="overflow-x-auto">
        <table class="data-table" data-test="bf-runs">
          <thead>
            <tr>
              <th scope="col">Run</th>
              <th scope="col">Dates</th>
              <th scope="col">Started by</th>
              <th scope="col">Status</th>
              <th scope="col">Progress</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="run in runs.runs" :key="run.run_id" class="cursor-pointer" tabindex="0"
              :class="run.run_id === selectedRun ? 'bg-brand-50/50' : ''"
              @click="selectedRun = run.run_id" @keydown.enter="selectedRun = run.run_id"
            >
              <td class="font-mono text-xs">{{ shortId(run.run_id) }}</td>
              <td class="whitespace-nowrap">{{ businessDate(run.date_from) }} – {{ businessDate(run.date_to) }}</td>
              <td>
                <p>{{ run.requested_by }}</p>
                <p class="text-2xs text-muted">{{ cairoDateTime(run.created_at) }}</p>
              </td>
              <td>
                <StatusBadge v-bind="backfillLabel.run(run.status)" />
                <p v-if="run.cancel_requested && !run.is_finished" class="text-2xs text-muted">cancelling…</p>
                <p v-else-if="!run.is_finished && run.days_discovered < run.coverage.length" class="text-2xs text-muted">discovering Teams {{ run.days_discovered }}/{{ run.coverage.length }}</p>
              </td>
              <td class="tabular-nums text-xs">
                <template v-if="run.counts">{{ run.counts.completed }} done · {{ run.counts.processing }} in progress · {{ run.counts.pending }} pending · {{ run.counts.blocked + run.counts.failed }} need attention</template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </SectionPanel>

    <!-- Run detail -->
    <SectionPanel
      v-if="selectedRun" :title="detail ? `Run ${shortId(detail.run.run_id)}` : 'Run'" flush
      :note="detail ? `${businessDate(detail.run.date_from)} – ${businessDate(detail.run.date_to)} · started by ${detail.run.requested_by} ${sinceNow(detail.run.started_at ?? detail.run.created_at)} · library ${detail.run.library_id}` : ''"
    >
      <p v-if="detailError" class="notice-error m-4">{{ detailError }}</p>
      <template v-else-if="detail">
        <div class="flex flex-wrap items-center gap-3 px-4 py-3">
          <StatusBadge v-bind="backfillLabel.run(detail.run.status)" />
          <span class="text-xs text-muted">Teams discovery {{ detail.run.days_discovered }}/{{ detail.run.coverage.length }} days</span>
          <dl class="flex flex-wrap gap-x-5 gap-y-1 text-sm" data-test="bf-counters">
            <div v-for="counter in COUNTERS" :key="counter.key" class="flex items-baseline gap-1.5">
              <dt class="text-2xs uppercase tracking-wide text-muted">{{ counter.label }}</dt>
              <dd class="font-semibold tabular-nums text-ink">{{ detail.run.counts?.[counter.key] ?? 0 }}</dd>
            </div>
          </dl>
          <span class="ml-auto text-2xs text-muted">Updated {{ cairoDateTime(detail.generated_at) }}</span>
        </div>
        <div v-if="detail.operator" class="flex flex-wrap items-center gap-2 border-t border-line px-4 py-3">
          <button
            v-if="!detail.run.is_finished" type="button" class="btn-secondary btn-sm"
            :disabled="acting || detail.run.cancel_requested" data-test="bf-cancel" @click="cancelRun"
          >
            <AppIcon name="close" :size="14" /> {{ detail.run.cancel_requested ? 'Cancellation requested' : 'Cancel pending work' }}
          </button>
          <template v-if="detail.run.status !== 'CANCELLED' && ((detail.run.counts?.blocked ?? 0) + (detail.run.counts?.failed ?? 0)) > 0">
            <input v-model="retryReason" type="text" class="field field-sm min-w-[16rem]" maxlength="300" placeholder="Why retry now? (required)" aria-label="Retry reason">
            <button type="button" class="btn-secondary btn-sm" :disabled="acting || !retryReason.trim()" data-test="bf-retry" @click="retryRun">
              <AppIcon name="refresh" :size="14" /> Retry blocked &amp; failed
            </button>
          </template>
          <span v-if="actionMessage" class="text-xs text-muted" role="status">{{ actionMessage }}</span>
        </div>
        <div class="flex flex-wrap items-center gap-2 border-t border-line px-4 py-2">
          <label class="filter-field">
            <span class="filter-label">Show</span>
            <select v-model="itemFilter" class="field field-sm w-auto">
              <option value="">Every lecture</option>
              <option v-for="counter in COUNTERS.slice(1)" :key="counter.key" :value="counter.key.toUpperCase()">{{ counter.label }}</option>
              <option value="CANCELLED">Cancelled</option>
            </select>
          </label>
        </div>
        <div class="overflow-x-auto">
          <table class="data-table" data-test="bf-run-lectures">
            <thead>
              <tr>
                <th scope="col">Lecture</th>
                <th scope="col">Origin</th>
                <th scope="col">Status</th>
                <th scope="col">Teams → LMS Match → Bunny Import → Encoding → Captions → Release → LMS Playback</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="lecture in detailRows" :key="rowKey(lecture)">
                <td class="min-w-[14rem]">
                  <p class="font-semibold text-ink">{{ lecture.subject ?? 'Lecture' }}</p>
                  <p class="text-2xs text-muted">{{ businessDate(lecture.session_date) }} · {{ cairoTimeRange(lecture.scheduled_start, lecture.scheduled_end) }}</p>
                </td>
                <td>
                  <StatusBadge v-bind="backfillLabel.origin(lecture.origin)" />
                  <p v-if="lecture.sources" class="text-2xs text-muted">{{ provenance(lecture.sources) }}</p>
                  <p v-if="!lecture.registered" class="text-2xs text-muted">{{ lecture.origin === 'QA_ONLY' ? 'not a publisher lecture' : 'not registered yet' }}</p>
                </td>
                <td class="min-w-[11rem]">
                  <StatusBadge v-bind="backfillLabel.item(lecture.status)" />
                  <p v-if="lecture.reason" class="mt-0.5 max-w-xs text-2xs leading-snug text-muted" :title="lecture.reason_detail">{{ QA_ONLY_CODE[lecture.reason] ?? lecture.reason }}</p>
                </td>
                <td>
                  <ol v-if="lecture.pipeline.length" class="flex flex-wrap items-center gap-1" :aria-label="`Pipeline of ${lecture.subject ?? 'lecture'}`">
                    <li
                      v-for="step in lecture.pipeline" :key="step.key"
                      class="rounded px-1.5 py-0.5 text-2xs font-semibold"
                      :class="step.done ? 'bg-emerald-50 text-emerald-800' : step.failed ? 'bg-rose-50 text-rose-700' : 'bg-slate-100 text-muted'"
                      :title="`${step.label}: ${step.status}`"
                    >
                      {{ step.label }}
                    </li>
                  </ol>
                  <span v-else class="text-2xs text-muted">Registered from Teams when the run discovers its day.</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
      <div v-else class="skeleton m-4 h-16" />
    </SectionPanel>

    <!-- Confirmation -->
    <div
      v-if="confirming && preview && preview.summary" class="fixed inset-0 z-50 grid place-items-center bg-brand-950/60 p-4 backdrop-blur-sm"
      role="presentation" @click.self="confirming = false"
    >
      <section class="surface max-h-[88vh] w-full max-w-lg overflow-y-auto" role="dialog" aria-modal="true" aria-label="Run historical backfill">
        <div class="panel-head">
          <div>
            <p class="kicker">Production publishing</p>
            <h2 class="mt-1 text-lg font-bold text-ink">Run historical backfill</h2>
          </div>
          <button type="button" class="icon-btn" aria-label="Close" @click="confirming = false"><AppIcon name="close" :size="16" /></button>
        </div>
        <div class="panel-body">
          <dl class="space-y-3 text-sm" data-test="bf-confirmation">
            <div><dt class="eyebrow">Date range (both dates included)</dt><dd class="mt-0.5 font-semibold text-ink">{{ businessDate(preview.preview.date_from) }} – {{ businessDate(preview.preview.date_to) }} · {{ preview.preview.coverage.length }} day{{ preview.preview.coverage.length === 1 ? '' : 's' }}</dd></div>
            <div>
              <dt class="eyebrow">Lectures to process</dt>
              <dd class="mt-0.5 font-semibold text-ink">{{ preview.summary.eligible }} ({{ preview.summary.new }} new from Teams, {{ preview.summary.resume }} to resume)</dd>
            </div>
            <div>
              <dt class="eyebrow">Estimated new Bunny imports</dt>
              <dd class="mt-0.5 font-semibold text-ink">
                {{ preview.summary.estimated_imports.known }}<template v-if="preview.summary.estimated_imports.possible"> known, up to {{ preview.summary.estimated_imports.upper_bound }} once recordings are located</template>
              </dd>
            </div>
            <div><dt class="eyebrow">Production Bunny library</dt><dd class="mt-0.5 font-semibold tabular-nums text-ink">{{ preview.preview.library_id }}</dd></div>
          </dl>
          <p class="mt-4 text-sm leading-6 text-muted">
            The publisher service first registers the range's Teams lectures exactly as daily publishing does, then re-checks every
            LMS match and recording and works through them in small batches, after daily publishing. Only the longest verified Teams
            recording of a lecture is imported; nothing already in Bunny is uploaded again, and release follows the usual rules.
          </p>
          <label class="mt-4 block">
            <span class="eyebrow">Reason (optional)</span>
            <input v-model="confirmReason" type="text" maxlength="300" class="field mt-1 w-full" placeholder="e.g. September recordings for the LMS">
          </label>
          <p v-if="startError" class="notice-error mt-3" role="alert">{{ startError }}</p>
        </div>
        <div class="flex justify-end gap-2 border-t border-line px-5 py-3">
          <button type="button" class="btn-secondary" :disabled="starting" @click="confirming = false">Cancel</button>
          <button type="button" class="btn-primary" :disabled="starting" data-test="bf-confirm" @click="confirmRun">
            {{ starting ? 'Starting…' : `Run backfill for ${preview.summary.eligible} lecture${preview.summary.eligible === 1 ? '' : 's'}` }}
          </button>
        </div>
      </section>
    </div>
  </div>
</template>
