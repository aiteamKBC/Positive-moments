<script setup lang="ts">
/**
 * Operations: what needs attention today.
 *
 * Every number on this page is computed by the platform and rendered
 * verbatim. Nothing here counts lectures, decides what "complete" means or
 * infers a status from a stage - that is `app.orchestration`'s job, and a
 * browser-side second opinion is a second opinion that drifts.
 *
 * THE HIERARCHY
 * -------------
 * One headline (business lectures), four outcome counts, then the operational
 * sections, then the exceptions, then the register, then the audit trail. The
 * previous version gave fifteen numbers equal weight, which is the same as
 * giving none of them any.
 *
 * "Business Lectures" is deliberately the headline rather than the canonical
 * count: a suppressed duplicate calendar event stays visible for audit but is
 * not a lecture anybody has to process.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppIcon from '../components/AppIcon.vue'
import AttendanceCell from '../components/operations/AttendanceCell.vue'
import DateNav from '../components/operations/DateNav.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import LoadingSkeleton from '../components/LoadingSkeleton.vue'
import PageHeader from '../components/PageHeader.vue'
import RunsTable from '../components/operations/RunsTable.vue'
import SectionPanel from '../components/SectionPanel.vue'
import StageChip from '../components/operations/StageChip.vue'
import StatTile from '../components/StatTile.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { getCalendar, getDay, getDirectory, getRuns, reconcileDay } from '../services/operations'
import type { DayReport, DirectoryEntry, LectureRow, PipelineRun } from '../types/operations'
import { businessDate, businessDateLong, cairoTimeRange, shiftBusinessDate } from '../utils/datetime'
import { actionLabel, actionTone, bucketLabel, bucketTone } from '../utils/labels'

const route = useRoute()
const router = useRouter()

const day = ref<DayReport | null>(null)
const runs = ref<PipelineRun[]>([])
const directory = ref<Record<string, DirectoryEntry>>({})
const latestLectureDate = ref<string | null>(null)
const loading = ref(true)
const error = ref('')
const showSuppressed = ref(false)
const reconciling = ref(false)
const notice = ref('')
const expandedRun = ref<string | null>(null)

const selectedDate = computed(() => (typeof route.query.date === 'string' ? route.query.date : undefined))

async function load() {
  loading.value = true
  error.value = ''
  notice.value = ''
  try {
    // The day report resolves fifteen stages per lecture and is the slow read.
    // The cheap ones run beside it rather than after it.
    const [report, recent] = await Promise.all([getDay(selectedDate.value), getRuns(6)])
    day.value = report
    runs.value = recent.runs
    // "Today" comes from the server, in Africa/Cairo, never from this browser.
    const today = report.navigation.today
    const [entries, calendar] = await Promise.all([
      getDirectory(report.session_date, report.session_date),
      getCalendar(shiftBusinessDate(today, -365), today),
    ])
    directory.value = Object.fromEntries(entries.lectures.map((row) => [row.lecture_id, row]))
    latestLectureDate.value = calendar.latest_lecture_date
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'The day could not be loaded.'
  } finally {
    loading.value = false
  }
}

function navigate(date: string) {
  router.push({ name: 'operations', query: { date } })
}

async function reconcile() {
  if (!day.value) return
  reconciling.value = true
  try {
    await reconcileDay(day.value.session_date)
    await load()
    notice.value = 'Reconciled from persisted state. This read nothing external and started no pipeline work.'
  } catch {
    notice.value = 'Reconciliation could not be completed. No pipeline work was started.'
  } finally {
    reconciling.value = false
  }
}

onMounted(load)
watch(selectedDate, load)

// The server owns the classification. The UI only places the rows carrying its
// explicit suppression flag somewhere other than the lecture table.
const lectures = computed(() => day.value?.lectures.filter((row) => !row.is_suppressed_duplicate) ?? [])
const suppressed = computed(() => day.value?.lectures.filter((row) => row.is_suppressed_duplicate) ?? [])
const attention = computed(() => lectures.value.filter((row) => row.bucket !== 'complete'))
const isEmptyDay = computed(() => Boolean(day.value) && day.value?.canonical_lecture_count === 0)

function trainerOf(row: LectureRow) {
  return directory.value[row.lecture_id]?.trainer ?? null
}

function timeOf(row: LectureRow) {
  const entry = directory.value[row.lecture_id]
  return cairoTimeRange(entry?.scheduled_start ?? row.scheduled_start, entry?.scheduled_end)
}
</script>

<template>
  <div class="page stack">
    <PageHeader
      kicker="Operations"
      title="Daily operations"
      lede="Pipeline health, exceptions and guarded actions for one Kent Business College teaching day."
    >
      <template #actions>
        <RouterLink class="btn-secondary" :to="{ name: 'operations-runs' }">
          <AppIcon name="clock" :size="16" /> Run history
        </RouterLink>
        <button
          type="button"
          class="btn-secondary"
          :disabled="!day || reconciling"
          title="Recompute this day from persisted state. Read-only."
          @click="reconcile"
        >
          <AppIcon name="refresh" :size="16" /> {{ reconciling ? 'Reconciling…' : 'Reconcile' }}
        </button>
      </template>
    </PageHeader>

    <p v-if="notice" class="notice" role="status">
      <AppIcon name="info" :size="16" class="mt-0.5" />{{ notice }}
    </p>

    <DateNav v-if="day" :navigation="day.navigation" :loading="loading" @navigate="navigate" />

    <template v-if="loading">
      <LoadingSkeleton variant="tiles" />
      <LoadingSkeleton />
    </template>
    <ErrorState v-else-if="error" :message="error" @retry="load" />

    <template v-else-if="day">
      <!-- An empty day is a normal answer, not an unfinished page. -->
      <section v-if="isEmptyDay" class="surface">
        <EmptyState
          icon="calendar"
          tone="neutral"
          title="No lectures scheduled or processed for this date"
          :message="`Nothing was discovered on the teaching calendar for ${businessDateLong(day.session_date)}.`"
        >
          <template #action>
            <button
              v-if="latestLectureDate && latestLectureDate !== day.session_date"
              type="button"
              class="btn-primary"
              @click="navigate(latestLectureDate)"
            >
              <AppIcon name="arrowRight" :size="16" />
              View latest lecture day — {{ businessDate(latestLectureDate) }}
            </button>
            <button
              v-if="!day.navigation.is_today"
              type="button"
              class="btn-secondary"
              @click="navigate(day.navigation.today)"
            >
              Back to today
            </button>
          </template>
        </EmptyState>
      </section>

      <template v-else>
        <!-- headline -->
        <section class="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <StatTile
            lead
            label="Business lectures"
            :value="day.business_lecture_count"
            :note="day.suppressed_duplicate_count
              ? `${day.canonical_lecture_count} calendar events · ${day.suppressed_duplicate_count} duplicate suppressed`
              : `${day.canonical_lecture_count} calendar events`"
          />
          <StatTile
            label="Complete" :value="day.complete_count" tone="ok" icon="check"
            note="Everything the platform owns is done."
          />
          <StatTile
            label="Waiting" :value="day.waiting_count" tone="waiting" icon="clock"
            note="An external source still owes us something."
          />
          <StatTile
            label="Needs review" :value="day.review_count" tone="review" icon="alert"
            note="The platform refused to guess. A person decides."
          />
          <StatTile
            label="Failed" :value="day.failed_count" tone="error" icon="slash"
            note="Errored, and a change is needed to move on."
          />
        </section>

        <!-- operational sections -->
        <section class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <RouterLink
            class="tile transition hover:border-brand-300 hover:shadow-raised"
            :to="{ name: 'pending-attendance', query: { date: day.session_date } }"
          >
            <p class="tile-label">Attendance attention</p>
            <p class="tile-value" :class="day.attendance_waiting_count ? 'text-amber-700' : 'text-faint'">
              {{ day.attendance_waiting_count }}
            </p>
            <p class="tile-note">Waiting on the external source. Waiting is not a failure.</p>
            <span class="mt-2 inline-flex items-center gap-1 text-xs font-semibold text-brand-700">
              Open queue <AppIcon name="arrowRight" :size="13" />
            </span>
          </RouterLink>

          <div class="tile">
            <p class="tile-label">Perfect lectures</p>
            <dl class="mt-2 space-y-1 text-sm">
              <div class="flex justify-between"><dt class="text-muted">Eligible</dt><dd class="font-semibold tabular-nums">{{ day.perfect_eligible_count }}</dd></div>
              <div class="flex justify-between"><dt class="text-muted">Synced</dt><dd class="font-semibold tabular-nums">{{ day.perfect_synced_count }}</dd></div>
              <div class="flex justify-between"><dt class="text-muted">Pending attendance</dt><dd class="font-semibold tabular-nums">{{ day.perfect_pending_attendance_count }}</dd></div>
            </dl>
          </div>

          <div class="tile">
            <p class="tile-label">Recording</p>
            <dl class="mt-2 space-y-1 text-sm">
              <div class="flex justify-between"><dt class="text-muted">Awaiting link</dt><dd class="font-semibold tabular-nums">{{ day.recording_missing_count }}</dd></div>
              <div class="flex justify-between"><dt class="text-muted">Excel pending</dt><dd class="font-semibold tabular-nums">{{ day.excel_pending_count }}</dd></div>
            </dl>
            <p class="tile-note">Both are owned by the live legacy workflows.</p>
          </div>

          <div class="tile">
            <p class="tile-label">Legacy QA sync</p>
            <dl class="mt-2 space-y-1 text-sm">
              <div class="flex justify-between"><dt class="text-muted">Synced</dt><dd class="font-semibold tabular-nums">{{ day.legacy_synced_count }}</dd></div>
              <div class="flex justify-between"><dt class="text-muted">Not coded-owned</dt><dd class="font-semibold tabular-nums">{{ day.legacy_not_coded_owned_count }}</dd></div>
              <div class="flex justify-between"><dt class="text-muted">Duplicates suppressed</dt><dd class="font-semibold tabular-nums">{{ day.suppressed_duplicate_count }}</dd></div>
            </dl>
          </div>
        </section>

        <!-- exceptions first -->
        <SectionPanel
          title="Lectures requiring attention"
          :count="attention.length || undefined"
          note="Anything the platform has not settled for this day."
          flush
        >
          <template #actions>
            <RouterLink class="btn-quiet" :to="{ name: 'manual-review', query: { date: day.session_date } }">
              Manual review <AppIcon name="arrowRight" :size="13" />
            </RouterLink>
          </template>
          <EmptyState
            v-if="!attention.length"
            compact
            title="Nothing is waiting on this day"
            message="Every business lecture has settled. Anything still open belongs to a legacy workflow and is reported above."
          />
          <table v-else class="data-table">
            <thead>
              <tr>
                <th scope="col">Lecture</th>
                <th scope="col">Status</th>
                <th scope="col">Current step</th>
                <th scope="col">What happens next</th>
                <th scope="col">Attendance</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in attention" :key="row.lecture_id">
                <td>
                  <RouterLink class="row-link" :to="{ name: 'lecture', params: { lectureId: row.lecture_id } }">
                    {{ row.subject }}
                  </RouterLink>
                  <p v-if="trainerOf(row)" class="text-xs text-muted">{{ trainerOf(row) }}</p>
                </td>
                <td><StatusBadge :label="bucketLabel(row.bucket)" :tone="bucketTone(row.bucket)" :title="row.bucket" /></td>
                <td class="whitespace-nowrap text-muted">{{ row.blocking_stage ? actionLabel(row.blocking_stage) : '—' }}</td>
                <td>
                  <StatusBadge
                    :label="actionLabel(row.next_executable_action)"
                    :tone="actionTone(row.next_executable_action)"
                    :title="row.next_executable_action"
                  />
                </td>
                <td>
                  <AttendanceCell
                    compact
                    :authoritative="row.attendance_source_authoritative"
                    :coverage-status="row.attendance_coverage_status"
                  />
                </td>
              </tr>
            </tbody>
          </table>
        </SectionPanel>

        <!-- the day's register -->
        <SectionPanel
          :title="`All lectures on ${businessDate(day.session_date)}`"
          :count="lectures.length"
          flush
        >
          <template #actions>
            <RouterLink class="btn-quiet" :to="{ name: 'lectures', query: { date: day.session_date } }">
              Open in Lectures <AppIcon name="arrowRight" :size="13" />
            </RouterLink>
          </template>
          <div class="overflow-x-auto">
            <table class="data-table min-w-[1000px]">
              <thead>
                <tr>
                  <th scope="col">Lecture</th>
                  <th scope="col">Time</th>
                  <th scope="col">Status</th>
                  <th scope="col">Attendance</th>
                  <th scope="col">QA</th>
                  <th scope="col">Perfect</th>
                  <th scope="col">Recording</th>
                  <th scope="col">Next</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in lectures" :key="row.lecture_id">
                  <td class="max-w-sm">
                    <RouterLink class="row-link" :to="{ name: 'lecture', params: { lectureId: row.lecture_id } }">
                      {{ row.subject }}
                    </RouterLink>
                    <p v-if="trainerOf(row)" class="text-xs text-muted">{{ trainerOf(row) }}</p>
                  </td>
                  <td class="whitespace-nowrap text-muted">{{ timeOf(row) || '—' }}</td>
                  <td><StatusBadge :label="bucketLabel(row.bucket)" :tone="bucketTone(row.bucket)" :title="row.bucket" /></td>
                  <td>
                    <AttendanceCell
                      compact
                      :authoritative="row.attendance_source_authoritative"
                      :coverage-status="row.attendance_coverage_status"
                    />
                  </td>
                  <td><StageChip compact :state="row.stages.QA_EVALUATION" /></td>
                  <td><StageChip compact :state="row.stages.PERFECT_ELIGIBILITY" /></td>
                  <td><StageChip compact :state="row.stages.RECORDING_LINK" /></td>
                  <td class="whitespace-nowrap text-xs text-muted">{{ actionLabel(row.next_executable_action) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </SectionPanel>

        <!-- audit: never hidden, never counted as a lecture -->
        <section v-if="suppressed.length">
          <button type="button" class="btn-quiet" @click="showSuppressed = !showSuppressed">
            <AppIcon :name="showSuppressed ? 'chevronDown' : 'chevronRight'" :size="14" />
            {{ showSuppressed ? 'Hide' : 'Show' }} {{ suppressed.length }} suppressed duplicate calendar
            event{{ suppressed.length === 1 ? '' : 's' }}
          </button>
          <div v-if="showSuppressed" class="table-wrap mt-2">
            <table class="data-table">
              <thead>
                <tr>
                  <th scope="col">Source calendar event</th>
                  <th scope="col">Outcome</th>
                  <th scope="col">Lecture that was kept</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in suppressed" :key="row.lecture_id">
                  <td>
                    <RouterLink class="row-link" :to="{ name: 'lecture', params: { lectureId: row.lecture_id } }">
                      {{ row.subject }}
                    </RouterLink>
                  </td>
                  <td><StatusBadge label="Duplicate calendar event suppressed" tone="quiet" /></td>
                  <td>
                    <RouterLink
                      v-if="row.duplicate_winner_lecture_id"
                      class="btn-quiet"
                      :to="{ name: 'lecture', params: { lectureId: row.duplicate_winner_lecture_id } }"
                    >
                      Open the kept lecture <AppIcon name="arrowRight" :size="13" />
                    </RouterLink>
                    <span v-else class="text-muted">—</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <!-- audit trail -->
        <SectionPanel title="Recent pipeline runs" flush>
          <template #actions>
            <RouterLink class="btn-quiet" :to="{ name: 'operations-runs' }">
              Full run history <AppIcon name="arrowRight" :size="13" />
            </RouterLink>
          </template>
          <EmptyState v-if="!runs.length" compact tone="neutral" icon="clock" title="No pipeline runs recorded yet" />
          <RunsTable
            v-else
            compact
            :runs="runs"
            :expanded-id="expandedRun"
            @toggle="(id) => (expandedRun = expandedRun === id ? null : id)"
          />
        </SectionPanel>
      </template>
    </template>
  </div>
</template>
