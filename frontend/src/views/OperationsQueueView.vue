<script setup lang="ts">
/**
 * The three attention queues, sharing one design.
 *
 * Pending attendance, manual review and errors answer different questions but
 * have the same shape, so they are one component with one table. Giving each
 * its own page was how the console started looking like three products.
 *
 * Each queue's empty state is written as GOOD news, because that is what it
 * is: an empty error queue is the outcome the platform is working towards.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppIcon from '../components/AppIcon.vue'
import AttendanceCell from '../components/operations/AttendanceCell.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import LoadingSkeleton from '../components/LoadingSkeleton.vue'
import PageHeader from '../components/PageHeader.vue'
import SectionPanel from '../components/SectionPanel.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { getErrors, getPendingAttendance, getReviewRequired } from '../services/operations'
import type { LectureListSlice } from '../types/operations'
import { businessDate, businessDateLong } from '../utils/datetime'
import { actionLabel, actionTone, bucketLabel, bucketTone, humanise, stageLabel } from '../utils/labels'

const route = useRoute()
const router = useRouter()

const data = ref<LectureListSlice | null>(null)
const loading = ref(true)
const error = ref('')

const date = computed(() => (typeof route.query.date === 'string' ? route.query.date : undefined))
const queue = computed(() => String(route.meta.queue ?? 'attendance'))

const COPY: Record<string, { title: string; lede: string; empty: string; emptyNote: string }> = {
  attendance: {
    title: 'Pending attendance',
    lede: 'Lectures whose attendance source has not answered yet. Waiting is not a failure — the next move belongs to the source, not to us.',
    empty: 'Every lecture has an authoritative attendance source',
    emptyNote: 'Nothing on this day is waiting on the attendance source.',
  },
  review: {
    title: 'Manual review',
    lede: 'Lectures the platform has explicitly marked for a human decision. It refuses to guess rather than proceeding on an assumption.',
    empty: 'No lecture needs a human decision',
    emptyNote: 'Nothing on this day has been marked for manual review.',
  },
  errors: {
    title: 'Errors',
    lede: 'Failed lectures only. Waiting, review-required and not-applicable states are deliberately excluded — none of them is a failure.',
    empty: 'No failures on this day',
    emptyNote: 'Every lecture either settled or is waiting on something outside the platform.',
  },
}

const copy = computed(() => COPY[queue.value] ?? COPY.attendance)
const rows = computed(() => data.value?.lectures.filter((row) => !row.is_suppressed_duplicate) ?? [])

async function load() {
  loading.value = true
  error.value = ''
  try {
    const fetcher = queue.value === 'review' ? getReviewRequired
      : queue.value === 'errors' ? getErrors : getPendingAttendance
    data.value = await fetcher(date.value)
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'This queue could not be loaded.'
  } finally {
    loading.value = false
  }
}
onMounted(load)
watch([queue, date], load)

function pickDate(event: Event) {
  router.replace({ query: { date: (event.target as HTMLInputElement).value } })
}

/**
 * The reason that explains why the lecture is HERE.
 *
 * A lecture can carry several reason codes at once, and the first one in the
 * object is whichever stage happened to be serialised first - which is how
 * "no terminal evaluation" ended up explaining a lecture that is actually
 * waiting on attendance. The blocking stage's own reason wins when it has one.
 */
function reasonFor(row: { reason_codes?: Record<string, string>; blocking_stage: string | null }) {
  const codes = row.reason_codes ?? {}
  const blocking = row.blocking_stage
  if (blocking && codes[blocking]) return { stage: blocking, code: codes[blocking] }
  const entries = Object.entries(codes)
  return entries.length ? { stage: entries[0][0], code: entries[0][1] } : null
}
</script>

<template>
  <div class="page stack">
    <RouterLink class="btn-quiet" :to="{ name: 'operations', query: date ? { date } : {} }">
      <AppIcon name="chevronLeft" :size="14" /> Operations
    </RouterLink>

    <PageHeader kicker="Operations" :title="copy.title" :lede="copy.lede">
      <template #meta>
        <p v-if="data" class="mt-2 text-xs text-muted">{{ businessDateLong(data.session_date) }}</p>
      </template>
      <template #actions>
        <label class="sr-only" for="queue-date">Business day</label>
        <input
          id="queue-date"
          type="date"
          class="field !w-auto"
          :value="date || data?.session_date"
          @change="pickDate"
        />
      </template>
    </PageHeader>

    <LoadingSkeleton v-if="loading" :rows="5" />
    <ErrorState v-else-if="error" :message="error" @retry="load" />

    <SectionPanel v-else :title="copy.title" :count="rows.length" flush>
      <EmptyState v-if="!rows.length" :title="copy.empty" :message="copy.emptyNote" />
      <div v-else class="overflow-x-auto">
        <table class="data-table min-w-[900px]">
          <thead>
            <tr>
              <th scope="col">Lecture</th>
              <th scope="col">Date</th>
              <th scope="col">Status</th>
              <th scope="col">Why</th>
              <th v-if="queue === 'attendance'" scope="col">Attendance</th>
              <th scope="col">What happens next</th>
              <th scope="col"><span class="sr-only">Open</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.lecture_id">
              <td class="max-w-sm">
                <RouterLink class="row-link" :to="{ name: 'lecture', params: { lectureId: row.lecture_id } }">
                  {{ row.subject }}
                </RouterLink>
              </td>
              <td class="whitespace-nowrap text-muted">{{ businessDate(row.session_date) }}</td>
              <td>
                <StatusBadge :label="bucketLabel(row.bucket)" :tone="bucketTone(row.bucket)" :title="row.bucket" />
              </td>
              <td class="max-w-sm">
                <template v-if="reasonFor(row)">
                  <p class="text-sm text-body">{{ humanise(reasonFor(row)!.code) }}</p>
                  <p class="text-2xs text-faint">at {{ stageLabel(reasonFor(row)!.stage) }}</p>
                </template>
                <span v-else-if="row.blocking_stage" class="text-sm text-muted">
                  {{ stageLabel(row.blocking_stage) }}
                </span>
                <span v-else class="text-muted">—</span>
              </td>
              <td v-if="queue === 'attendance'">
                <AttendanceCell
                  compact
                  :authoritative="row.attendance_source_authoritative"
                  :coverage-status="row.attendance_coverage_status"
                />
              </td>
              <td>
                <StatusBadge
                  :label="actionLabel(row.next_executable_action)"
                  :tone="actionTone(row.next_executable_action)"
                  :title="row.next_executable_action"
                />
              </td>
              <td class="text-right">
                <RouterLink class="btn-quiet" :to="{ name: 'lecture', params: { lectureId: row.lecture_id } }">
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
