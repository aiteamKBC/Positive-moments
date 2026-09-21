<script setup lang="ts">
/**
 * Lectures: the operational lecture workspace.
 *
 * The question this page answers is "what is the current state of our
 * lectures?" - which is a different question from Operations ("what needs
 * attention today?") and from Positive Moments ("what did we find in them?").
 * Those three used to share a table; they no longer share anything but the
 * design system.
 *
 * WHERE THE DATA COMES FROM, AND WHY IT IS TWO CALLS
 * --------------------------------------------------
 * Operational state comes from `/operations/day/`, which resolves the full
 * fifteen-stage matrix and is the expensive read. Trainer, module and the
 * scheduled window come from `/operations/directory/`, which is one cheap
 * query. They are joined here on `lecture_id`, in the browser, purely to place
 * two server-provided values in the same row.
 *
 * NOTHING ON THIS PAGE DERIVES A STATE. The filters compare fields the server
 * sent; the counters sum figures the server computed. A bucket, a stage state
 * and an attendance verdict all arrive already decided.
 *
 * Only days the calendar says carry lectures are fetched, so widening the
 * range never spends a slow request on an empty day.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppIcon from '../components/AppIcon.vue'
import AttendanceCell from '../components/operations/AttendanceCell.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import FilterBar from '../components/FilterBar.vue'
import LoadingSkeleton from '../components/LoadingSkeleton.vue'
import PageHeader from '../components/PageHeader.vue'
import SectionPanel from '../components/SectionPanel.vue'
import StageChip from '../components/operations/StageChip.vue'
import StatTile from '../components/StatTile.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { getCalendar, getDay, getDirectory } from '../services/operations'
import type { DayReport, DirectoryEntry, LectureRow } from '../types/operations'
import { businessDate, cairoTimeRange, shiftBusinessDate } from '../utils/datetime'
import { actionLabel, bucketLabel, bucketTone, stageLabel } from '../utils/labels'

type WorkspaceRow = LectureRow & { entry?: DirectoryEntry }

const route = useRoute()
const router = useRouter()

const reports = ref<DayReport[]>([])
const rows = ref<WorkspaceRow[]>([])
const loadedDates = ref<string[]>([])
const loading = ref(true)
const error = ref('')

const SPANS = [
  { value: '1', label: 'Latest teaching day' },
  { value: '3', label: 'Last 3 teaching days' },
  { value: '7', label: 'Last 7 teaching days' },
]

const filters = reactive({
  search: '', trainer: '', status: '', attendance: '', qa: '', perfect: '', recording: '',
})
const span = ref('1')

/** A pinned date (arrived from Operations) overrides the span selector. */
const pinnedDate = computed(() => (typeof route.query.date === 'string' ? route.query.date : ''))

async function load() {
  loading.value = true
  error.value = ''
  try {
    let dates: string[]
    if (pinnedDate.value) {
      dates = [pinnedDate.value]
    } else {
      const today = new Date().toISOString().slice(0, 10)
      const calendar = await getCalendar(shiftBusinessDate(today, -365), today)
      dates = calendar.days
        .filter((day) => day.business_lecture_count > 0)
        .slice(0, Number(span.value))
        .map((day) => day.session_date)
    }
    if (!dates.length) {
      reports.value = []; rows.value = []; loadedDates.value = []
      return
    }
    const sorted = [...dates].sort()
    const [days, directory] = await Promise.all([
      Promise.all(dates.map((date) => getDay(date))),
      getDirectory(sorted[0], sorted[sorted.length - 1]),
    ])
    const entries = new Map(directory.lectures.map((entry) => [entry.lecture_id, entry]))
    reports.value = days
    loadedDates.value = dates
    rows.value = days
      .flatMap((day) => day.lectures)
      .filter((row) => !row.is_suppressed_duplicate)
      .map((row) => ({ ...row, entry: entries.get(row.lecture_id) }))
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'The lecture workspace could not be loaded.'
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch([span, pinnedDate], load)

/** Summed from what each day report already decided. Nothing is recounted. */
const totals = computed(() => reports.value.reduce((sum, day) => ({
  lectures: sum.lectures + day.business_lecture_count,
  complete: sum.complete + day.complete_count,
  waiting: sum.waiting + day.waiting_count,
  review: sum.review + day.review_count,
  failed: sum.failed + day.failed_count,
}), { lectures: 0, complete: 0, waiting: 0, review: 0, failed: 0 }))

const trainers = computed(() => [...new Set(
  rows.value.map((row) => row.entry?.trainer).filter((name): name is string => Boolean(name)),
)].sort())

const visible = computed(() => rows.value.filter((row) => {
  const term = filters.search.trim().toLowerCase()
  if (term && !`${row.subject} ${row.entry?.trainer ?? ''}`.toLowerCase().includes(term)) return false
  if (filters.trainer && row.entry?.trainer !== filters.trainer) return false
  if (filters.status && row.bucket !== filters.status) return false
  if (filters.attendance === 'authoritative' && !row.attendance_source_authoritative) return false
  if (filters.attendance === 'waiting' && row.attendance_source_authoritative) return false
  if (filters.qa && row.stages.QA_EVALUATION !== filters.qa) return false
  if (filters.perfect && row.stages.PERFECT_ELIGIBILITY !== filters.perfect) return false
  if (filters.recording === 'available' && row.stages.RECORDING_LINK !== 'COMPLETE') return false
  if (filters.recording === 'pending' && row.stages.RECORDING_LINK === 'COMPLETE') return false
  return true
}))

const activeCount = computed(() => Object.values(filters).filter(Boolean).length)
const hiddenActive = computed(
  () => [filters.qa, filters.perfect, filters.recording, filters.attendance].filter(Boolean).length,
)

function reset() {
  Object.assign(filters, { search: '', trainer: '', status: '', attendance: '', qa: '', perfect: '', recording: '' })
}

function clearPinnedDate() {
  router.replace({ name: 'lectures' })
}

function moduleOf(row: WorkspaceRow) {
  const value = row.entry?.module ?? row.module
  return value && value !== row.subject ? value : null
}
</script>

<template>
  <div class="page stack">
    <PageHeader
      kicker="Lectures"
      title="Lecture workspace"
      lede="Every discovered lecture with its trainer, its scheduled slot and the operational state the platform has resolved for it."
    >
      <template #actions>
        <label class="sr-only" for="span">Range</label>
        <select v-if="!pinnedDate" id="span" v-model="span" class="field !w-auto">
          <option v-for="option in SPANS" :key="option.value" :value="option.value">{{ option.label }}</option>
        </select>
        <button v-else type="button" class="btn-secondary" @click="clearPinnedDate">
          <AppIcon name="close" :size="15" /> Clear {{ businessDate(pinnedDate) }}
        </button>
      </template>
      <template #meta>
        <p v-if="loadedDates.length" class="mt-2 text-xs text-muted">
          Showing
          <span class="font-semibold text-body">{{ loadedDates.map((d) => businessDate(d)).join(' · ') }}</span>
          — only days that carry lectures are loaded.
        </p>
      </template>
    </PageHeader>

    <section class="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
      <StatTile
        lead label="Lectures" :value="totals.lectures" :loading="loading"
        :note="`Across ${loadedDates.length} teaching day${loadedDates.length === 1 ? '' : 's'}`"
      />
      <StatTile
        label="Complete" :value="totals.complete" tone="ok" icon="check" :loading="loading"
        note="Settled end to end."
      />
      <StatTile
        label="Waiting" :value="totals.waiting" tone="waiting" icon="clock" :loading="loading"
        note="Held by an external source."
      />
      <StatTile
        label="Needs review" :value="totals.review" tone="review" icon="alert" :loading="loading"
        note="Awaiting a human decision."
      />
      <StatTile
        label="Failed" :value="totals.failed" tone="error" icon="slash" :loading="loading"
        note="Errored during processing."
      />
    </section>

    <FilterBar :active-count="activeCount" :hidden-active="hiddenActive" @reset="reset">
      <div class="relative min-w-[14rem] flex-1">
        <AppIcon name="search" :size="16" class="pointer-events-none absolute left-3 top-2.5 text-faint" />
        <label class="sr-only" for="lecture-search">Search lectures</label>
        <input id="lecture-search" v-model="filters.search" class="field pl-9" placeholder="Search lecture or trainer" />
      </div>
      <div class="filter-field">
        <span class="filter-label">Trainer</span>
        <label class="sr-only" for="trainer">Trainer</label>
        <select id="trainer" v-model="filters.trainer" class="field !w-auto">
          <option value="">All</option>
          <option v-for="name in trainers" :key="name" :value="name">{{ name }}</option>
        </select>
      </div>
      <div class="filter-field">
        <span class="filter-label">Pipeline</span>
        <label class="sr-only" for="status">Pipeline status</label>
        <select id="status" v-model="filters.status" class="field !w-auto">
          <option value="">Any status</option>
          <option value="complete">Complete</option>
          <option value="waiting">Waiting</option>
          <option value="review">Needs review</option>
          <option value="failed">Failed</option>
          <option value="in_progress">In progress</option>
        </select>
      </div>

      <template #more>
        <div class="filter-field">
          <span class="filter-label">Attendance</span>
          <label class="sr-only" for="attendance">Attendance status</label>
          <select id="attendance" v-model="filters.attendance" class="field !w-auto">
            <option value="">Any</option>
            <option value="authoritative">Source available</option>
            <option value="waiting">Waiting for source</option>
          </select>
        </div>
        <div class="filter-field">
          <span class="filter-label">QA</span>
          <label class="sr-only" for="qa">QA status</label>
          <select id="qa" v-model="filters.qa" class="field !w-auto">
            <option value="">Any</option>
            <option value="COMPLETE">Evaluated</option>
            <option value="MISSING">Not started</option>
            <option value="REVIEW_REQUIRED">Review required</option>
            <option value="FAILED">Failed</option>
          </select>
        </div>
        <div class="filter-field">
          <span class="filter-label">Perfect</span>
          <label class="sr-only" for="perfect">Perfect status</label>
          <select id="perfect" v-model="filters.perfect" class="field !w-auto">
            <option value="">Any</option>
            <option value="COMPLETE">Decided</option>
            <option value="WAITING">Pending attendance</option>
            <option value="MISSING">Not evaluated</option>
          </select>
        </div>
        <div class="filter-field">
          <span class="filter-label">Recording</span>
          <label class="sr-only" for="recording">Recording availability</label>
          <select id="recording" v-model="filters.recording" class="field !w-auto">
            <option value="">Any</option>
            <option value="available">Link stored</option>
            <option value="pending">Link pending</option>
          </select>
        </div>
      </template>
    </FilterBar>

    <LoadingSkeleton v-if="loading" :rows="8" />
    <ErrorState v-else-if="error" :message="error" @retry="load" />

    <SectionPanel v-else title="Lectures" :count="`${visible.length} of ${rows.length}`" flush>
      <EmptyState
        v-if="!rows.length"
        tone="neutral"
        icon="calendar"
        title="No lectures in this range"
        message="Widen the range, or open Operations to pick a specific teaching day."
      />
      <EmptyState
        v-else-if="!visible.length"
        tone="neutral"
        icon="search"
        title="No lectures match these filters"
        message="Try clearing one of them."
      >
        <template #action><button type="button" class="btn-secondary" @click="reset">Reset filters</button></template>
      </EmptyState>
      <div v-else class="overflow-x-auto">
        <table class="data-table min-w-[1180px]">
          <thead>
            <tr>
              <th scope="col" class="w-[19rem]">Lecture</th>
              <th scope="col">Date &amp; time</th>
              <th scope="col">Trainer</th>
              <th scope="col">Attendance</th>
              <th scope="col">Engagement</th>
              <th scope="col">QA</th>
              <th scope="col">Perfect</th>
              <th scope="col">Recording</th>
              <th scope="col">Status</th>
              <th scope="col">Next action</th>
              <th scope="col"><span class="sr-only">Open</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in visible" :key="row.lecture_id">
              <td>
                <RouterLink class="row-link" :to="{ name: 'lecture', params: { lectureId: row.lecture_id } }">
                  {{ row.subject }}
                </RouterLink>
                <p v-if="moduleOf(row)" class="truncate text-xs text-muted">{{ moduleOf(row) }}</p>
              </td>
              <td class="whitespace-nowrap">
                <span class="block text-body">{{ businessDate(row.session_date) }}</span>
                <span class="block text-xs text-muted">
                  {{ cairoTimeRange(row.entry?.scheduled_start ?? row.scheduled_start, row.entry?.scheduled_end) || 'Time unavailable' }}
                </span>
              </td>
              <td class="whitespace-nowrap text-body">{{ row.entry?.trainer ?? 'Not recorded' }}</td>
              <td>
                <AttendanceCell
                  compact
                  :authoritative="row.attendance_source_authoritative"
                  :coverage-status="row.attendance_coverage_status"
                />
              </td>
              <td><StageChip compact :state="row.stages.ENGAGEMENT" /></td>
              <td><StageChip compact :state="row.stages.QA_EVALUATION" /></td>
              <td><StageChip compact :state="row.stages.PERFECT_ELIGIBILITY" /></td>
              <td><StageChip compact :state="row.stages.RECORDING_LINK" /></td>
              <td><StatusBadge :label="bucketLabel(row.bucket)" :tone="bucketTone(row.bucket)" :title="row.bucket" /></td>
              <td class="max-w-[13rem] text-xs text-muted">
                {{ actionLabel(row.next_executable_action) }}
                <span
                  v-if="row.blocking_stage && row.next_executable_action !== 'NOTHING_TO_DO'"
                  class="block text-2xs text-faint"
                >at {{ stageLabel(row.blocking_stage) }}</span>
              </td>
              <td class="text-right">
                <RouterLink
                  class="btn-quiet"
                  :to="{ name: 'lecture', params: { lectureId: row.lecture_id } }"
                  :aria-label="`Open ${row.subject}`"
                >
                  Open <AppIcon name="arrowRight" :size="13" />
                </RouterLink>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </SectionPanel>
  </div>
</template>
