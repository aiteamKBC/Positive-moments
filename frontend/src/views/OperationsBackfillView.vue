<script setup lang="ts">
/**
 * Historical Backfill.
 *
 * The job this page does: an operator knows a stretch of term was never
 * processed, and needs to recover it without being asked to understand the
 * pipeline. So the page is a two-step decision, not a control panel - look
 * first, then commit - and everything it shows is an answer rather than a
 * status code.
 *
 * Both steps queue a durable run. A September preview costs minutes of
 * Microsoft Graph reads, so neither step is something the browser waits on;
 * the page polls, and closing the tab changes nothing about the work.
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import AppIcon from '../components/AppIcon.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import LoadingSkeleton from '../components/LoadingSkeleton.vue'
import PageHeader from '../components/PageHeader.vue'
import SectionPanel from '../components/SectionPanel.vue'
import StatTile from '../components/StatTile.vue'
import StatusBadge from '../components/StatusBadge.vue'
import TechnicalDetails from '../components/TechnicalDetails.vue'
import {
  cancelBackfill,
  getBackfill,
  getBackfills,
  previewBackfill,
  startBackfill,
} from '../services/operations'
import { businessDate, businessDateLong, elapsed, sinceNow } from '../utils/datetime'
import { backfillDayStatus, backfillStatus } from '../utils/labels'
import type { BackfillDay, BackfillRun } from '../types/operations'

/** The mandatory first use case: September 2026 from the 1st. */
const DEFAULT_FROM = '2026-09-01'

function today(): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Africa/Cairo' }).format(new Date())
}

const from = ref(DEFAULT_FROM)
const to = ref(today())

const runs = ref<BackfillRun[]>([])
const active = ref<BackfillRun | null>(null)
const selected = ref<BackfillRun | null>(null)
const days = ref<BackfillDay[]>([])

const loading = ref(true)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const confirmingCancel = ref(false)

let poll: ReturnType<typeof setInterval> | undefined

const rangeDays = computed(() => {
  const start = Date.parse(from.value)
  const end = Date.parse(to.value)
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return 0
  return Math.round((end - start) / 86_400_000) + 1
})

const rangeProblem = computed(() => {
  if (!from.value || !to.value) return 'Choose both a start and an end date.'
  if (rangeDays.value === 0) return 'The end date is before the start date.'
  if (rangeDays.value > 120) return `${rangeDays.value} days is more than one run can cover (120 maximum).`
  return ''
})

/**
 * Start is unlocked only by a finished preview OF THE RANGE NOW SELECTED.
 *
 * Matching the dates matters: without it, previewing 16-18 September and then
 * widening the fields to 1-21 September would leave Start enabled, and the
 * operator would commit to three weeks believing they had reviewed them.
 */
const previewReady = computed(() =>
  selected.value?.mode === 'PREVIEW'
  && selected.value?.status === 'COMPLETED'
  && selected.value?.requested_from === from.value
  && selected.value?.requested_to === to.value)

const showRunning = computed(() =>
  selected.value !== null && !selected.value.is_finished)

const blockedByLegacy = computed(() =>
  selected.value?.status === 'BLOCKED_LEGACY_QA_ACTIVE')

const totals = computed(() => {
  const run = selected.value
  if (!run) return null
  return [
    { label: 'Matched lectures', value: run.matched_count },
    { label: 'Already complete', value: run.already_complete_count },
    { label: run.mode === 'PREVIEW' ? 'Newly discoverable' : 'Newly discovered', value: run.discovered_count },
    { label: 'Processed', value: run.processed_count, hide: run.mode === 'PREVIEW' },
    { label: 'Waiting', value: run.waiting_count },
    { label: 'Needs review', value: run.review_required_count },
    { label: 'Failed', value: run.failed_count },
    { label: 'Suppressed duplicates', value: run.suppressed_count },
  ].filter(tile => !tile.hide)
})

async function loadList() {
  try {
    const response = await getBackfills(25)
    runs.value = response.backfill_runs
    active.value = response.active
    if (!selected.value && response.active) await select(response.active.backfill_run_id)
    else if (!selected.value && response.backfill_runs.length) {
      await select(response.backfill_runs[0].backfill_run_id)
    }
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'Backfill history could not be loaded.'
  }
}

async function select(runId: string) {
  try {
    const response = await getBackfill(runId)
    selected.value = response.backfill_run
    days.value = response.days
    error.value = ''
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'That backfill run could not be loaded.'
  }
}

async function refresh() {
  await loadList()
  if (selected.value) await select(selected.value.backfill_run_id)
}

async function queue(kind: 'preview' | 'start') {
  if (rangeProblem.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    const response = kind === 'preview'
      ? await previewBackfill(from.value, to.value)
      : await startBackfill(from.value, to.value)
    notice.value = response.detail
    selected.value = response.backfill_run
    days.value = []
    await loadList()
  } catch (caught) {
    error.value = message(caught, 'The run could not be queued.')
  } finally {
    busy.value = false
  }
}

async function confirmCancel() {
  if (!selected.value) return
  busy.value = true
  try {
    const response = await cancelBackfill(selected.value.backfill_run_id)
    notice.value = response.detail
    confirmingCancel.value = false
    await refresh()
  } catch (caught) {
    error.value = message(caught, 'The run could not be cancelled.')
  } finally {
    busy.value = false
  }
}

function message(caught: unknown, fallback: string): string {
  const detail = (caught as { response?: { data?: { detail?: string } } })?.response?.data?.detail
  if (detail) return detail
  return caught instanceof Error ? caught.message : fallback
}

function label(day: BackfillDay) {
  return backfillDayStatus(day.status)
}

onMounted(async () => {
  await loadList()
  loading.value = false
  // Only while something is in flight; a finished run does not need polling.
  poll = setInterval(() => { if (showRunning.value) void refresh() }, 5000)
})

onBeforeUnmount(() => { if (poll) clearInterval(poll) })
</script>

<template>
  <div class="page">
    <PageHeader
      title="Historical backfill"
      kicker="Operations"
      description="Recover lectures from a past date range using the nightly pipeline, one business day at a time."
    />

    <ErrorState v-if="error" :message="error" @retry="refresh" />

    <LoadingSkeleton v-if="loading" :rows="4" />

    <template v-else>
      <!-- ---------------------------------------------------------------- -->
      <SectionPanel
        title="Choose a date range"
        note="Preview first. Nothing is written until you start a backfill."
      >
        <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4 lg:items-end">
          <label class="block">
            <span class="label">From</span>
            <input v-model="from" class="field" type="date" :disabled="busy" />
          </label>
          <label class="block">
            <span class="label">To</span>
            <input v-model="to" class="field" type="date" :disabled="busy" />
          </label>
          <p class="text-sm text-muted lg:pb-2">
            <template v-if="rangeProblem">{{ rangeProblem }}</template>
            <template v-else>
              {{ rangeDays }} business {{ rangeDays === 1 ? 'day' : 'days' }},
              {{ businessDate(from) }} to {{ businessDate(to) }}.
            </template>
          </p>
          <div class="flex flex-wrap gap-2 lg:justify-end lg:pb-1">
            <button
              class="btn-secondary"
              :disabled="busy || !!rangeProblem"
              @click="queue('preview')"
            >
              <AppIcon name="search" />
              Preview backfill
            </button>
            <button
              class="btn-primary"
              :disabled="busy || !!rangeProblem || !previewReady"
              :title="previewReady ? '' : 'Preview these exact dates first'"
              @click="queue('start')"
            >
              <AppIcon name="play" />
              Start backfill
            </button>
          </div>
        </div>

        <p v-if="notice" class="notice-info mt-4">{{ notice }}</p>
        <p v-if="!previewReady && !busy" class="notice-quiet mt-4">
          Start becomes available once a preview <em>of these exact dates</em>
          has finished, so the numbers are seen before anything is written.
        </p>
      </SectionPanel>

      <!-- ---------------------------------------------------------------- -->
      <SectionPanel
        v-if="selected"
        :title="selected.mode === 'PREVIEW' ? 'Preview result' : 'Backfill run'"
        :note="`${businessDateLong(selected.requested_from)} to ${businessDateLong(selected.requested_to)}`"
      >
        <template #actions>
          <StatusBadge
            :label="backfillStatus(selected.status).label"
            :tone="backfillStatus(selected.status).tone"
          />
        </template>

        <p v-if="blockedByLegacy" class="notice-error">
          The legacy n8n QA path is active, so coded QA writes were refused and
          this run stopped. That guard is deliberate: two writers on one lecture
          is the situation it exists to prevent.
        </p>

        <div class="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
          <span>
            <strong>{{ selected.completed_days }}</strong> of
            {{ selected.total_days }} days
          </span>
          <span v-if="showRunning && selected.current_business_date" class="text-muted">
            Working on {{ businessDate(selected.current_business_date) }}
          </span>
          <span v-if="selected.current_lecture_label" class="text-muted">
            {{ selected.current_lecture_label }}
          </span>
          <span v-if="selected.started_at" class="text-muted">
            Started {{ sinceNow(selected.started_at) }}
          </span>
          <span v-if="selected.finished_at && selected.started_at" class="text-muted">
            Took {{ elapsed(selected.started_at, selected.finished_at) }}
          </span>
        </div>

        <div
          class="mt-3 h-2 w-full overflow-hidden rounded-full bg-line"
          role="progressbar"
          :aria-valuenow="selected.progress_percent"
          aria-valuemin="0"
          aria-valuemax="100"
        >
          <div
            class="h-full rounded-full bg-brand-600 transition-all"
            :style="{ width: `${selected.progress_percent}%` }"
          />
        </div>

        <!-- `.tile-lead` is a single dark hero TILE, not a grid; the tiles
             belong in a responsive grid, as on the daily dashboard. -->
        <div v-if="totals" class="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile
            v-for="tile in totals"
            :key="tile.label"
            :label="tile.label"
            :value="String(tile.value)"
          />
        </div>

        <p v-if="selected.error_summary" class="notice-error mt-4">
          {{ selected.error_summary }}
        </p>

        <div v-if="showRunning" class="mt-5 flex flex-wrap gap-2">
          <button
            v-if="!confirmingCancel"
            class="btn-danger"
            :disabled="busy || selected.cancel_requested"
            @click="confirmingCancel = true"
          >
            {{ selected.cancel_requested ? 'Stopping after this day' : 'Cancel backfill' }}
          </button>
          <template v-else>
            <p class="notice-warning w-full">
              The day currently being processed will finish, and no further day
              will start. Work already written stays written.
            </p>
            <button class="btn-danger" :disabled="busy" @click="confirmCancel">
              Yes, cancel after this day
            </button>
            <button class="btn-quiet" :disabled="busy" @click="confirmingCancel = false">
              Keep running
            </button>
          </template>
        </div>
      </SectionPanel>

      <!-- ---------------------------------------------------------------- -->
      <SectionPanel v-if="days.length" title="Day by day">
        <div class="no-scrollbar overflow-x-auto">
          <table class="data-table">
            <thead>
              <tr>
                <th scope="col">Day</th>
                <th scope="col">State</th>
                <th scope="col" class="text-right">Calendar events</th>
                <th scope="col" class="text-right">Matched</th>
                <th scope="col" class="text-right">New</th>
                <th scope="col" class="text-right">Complete</th>
                <th scope="col" class="text-right">Processed</th>
                <th scope="col" class="text-right">Waiting</th>
                <th scope="col" class="text-right">Review</th>
                <th scope="col" class="text-right">Failed</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="day in days" :key="day.business_date">
                <th scope="row" class="whitespace-nowrap">
                  {{ businessDate(day.business_date) }}
                </th>
                <td>
                  <StatusBadge :label="label(day).label" :tone="label(day).tone" />
                  <span v-if="day.error_code" class="ml-2 text-2xs text-muted">
                    {{ day.error_code }}
                  </span>
                </td>
                <td class="text-right">{{ day.calendar_events_considered }}</td>
                <td class="text-right">{{ day.matched_lectures }}</td>
                <td class="text-right">{{ day.newly_discovered }}</td>
                <td class="text-right">{{ day.already_complete }}</td>
                <td class="text-right">{{ day.processed }}</td>
                <td class="text-right">{{ day.waiting }}</td>
                <td class="text-right">{{ day.review_required }}</td>
                <td class="text-right">{{ day.failed }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <TechnicalDetails summary="Technical detail">
          <dl class="grid gap-2 sm:grid-cols-2">
            <div v-if="selected">
              <dt>Run identifier</dt>
              <dd class="font-mono text-2xs">{{ selected.backfill_run_id }}</dd>
            </div>
            <div v-if="selected?.runner_version">
              <dt>Runner version</dt>
              <dd class="font-mono text-2xs">{{ selected.runner_version }}</dd>
            </div>
            <div v-if="selected?.created_by">
              <dt>Requested by</dt>
              <dd>{{ selected.created_by }}</dd>
            </div>
          </dl>
        </TechnicalDetails>
      </SectionPanel>

      <!-- ---------------------------------------------------------------- -->
      <SectionPanel title="Previous runs">
        <EmptyState
          v-if="!runs.length"
          title="No backfill has been run yet"
          description="Choose a date range above and preview it to see what a backfill would find."
        />
        <div v-else class="no-scrollbar overflow-x-auto">
          <table class="data-table">
            <thead>
              <tr>
                <th scope="col">Range</th>
                <th scope="col">Kind</th>
                <th scope="col">State</th>
                <th scope="col" class="text-right">Days</th>
                <th scope="col" class="text-right">Processed</th>
                <th scope="col" class="text-right">Waiting</th>
                <th scope="col" class="text-right">Review</th>
                <th scope="col" class="text-right">Failed</th>
                <th scope="col">Requested</th>
                <th scope="col"><span class="sr-only">Open</span></th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="run in runs"
                :key="run.backfill_run_id"
                :class="run.backfill_run_id === selected?.backfill_run_id ? 'bg-brand-50/60' : ''"
              >
                <th scope="row" class="whitespace-nowrap">
                  {{ businessDate(run.requested_from) }} – {{ businessDate(run.requested_to) }}
                </th>
                <td>{{ run.mode === 'PREVIEW' ? 'Preview' : 'Backfill' }}</td>
                <td>
                  <StatusBadge
                    :label="backfillStatus(run.status).label"
                    :tone="backfillStatus(run.status).tone"
                  />
                </td>
                <td class="text-right">{{ run.completed_days }} / {{ run.total_days }}</td>
                <td class="text-right">{{ run.processed_count }}</td>
                <td class="text-right">{{ run.waiting_count }}</td>
                <td class="text-right">{{ run.review_required_count }}</td>
                <td class="text-right">{{ run.failed_count }}</td>
                <td class="whitespace-nowrap text-muted">{{ sinceNow(run.created_at) }}</td>
                <td class="text-right">
                  <button class="btn-quiet" @click="select(run.backfill_run_id)">Open</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </SectionPanel>
    </template>
  </div>
</template>
