<script setup lang="ts">
/**
 * One lecture, in full. The single canonical detail page, keyed by lecture_id.
 *
 * The page states the platform's own answers and derives none of them. In
 * particular `retry_eligibility` and the recover-attendance plan come from the
 * server, so a disabled action is disabled for a reason the platform can name,
 * and a confirmation dialog shows the plan the server generated rather than
 * one this component imagined.
 *
 * There is no Force Reprocess here and there is no generic action runner. The
 * three guarded endpoints are the whole mutation surface.
 *
 * The tabs are prepared for Lecture Parts and Media, which do not exist yet -
 * so they are not shown. An empty tab promising future media would be a lie
 * told in navigation.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import ActionPlanDialog from '../components/ActionPlanDialog.vue'
import AppIcon from '../components/AppIcon.vue'
import AttendanceCell from '../components/operations/AttendanceCell.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import LoadingSkeleton from '../components/LoadingSkeleton.vue'
import PageHeader from '../components/PageHeader.vue'
import SectionPanel from '../components/SectionPanel.vue'
import StageChip from '../components/operations/StageChip.vue'
import StageMatrix from '../components/operations/StageMatrix.vue'
import StatusBadge from '../components/StatusBadge.vue'
import TabNav from '../components/TabNav.vue'
import TechnicalDetails from '../components/TechnicalDetails.vue'
import {
  getDirectory, getLecture, getRecoverAttendancePlan, getRetryPlan,
  recoverAttendance, retryLecture,
} from '../services/operations'
import type { DirectoryEntry, LectureDetail } from '../types/operations'
import { businessDate, businessDateLong, cairoDateTime, cairoTimeRange } from '../utils/datetime'
import {
  actionLabel, actionTone, bucketLabel, bucketTone, humanise, perfectReason,
  recordingOwner, recordingSource, retryReason, stageLabel, stageState,
} from '../utils/labels'

const route = useRoute()
const router = useRouter()

const lecture = ref<LectureDetail | null>(null)
const entry = ref<DirectoryEntry | null>(null)
const recoverPlan = ref<Record<string, unknown> | null>(null)
const loading = ref(true)
const error = ref('')
const notice = ref('')
const actionKind = ref<'Retry' | 'Recover attendance' | ''>('')
const actionPlan = ref<Record<string, unknown> | null>(null)
const actionBusy = ref(false)

const lectureId = computed(() => String(route.params.lectureId))
const tab = computed(() => (typeof route.query.tab === 'string' ? route.query.tab : 'overview'))

function selectTab(id: string) {
  router.replace({ query: { ...route.query, tab: id === 'overview' ? undefined : id } })
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const detail = await getLecture(lectureId.value)
    lecture.value = detail
    const directory = await getDirectory(detail.session_date, detail.session_date)
    entry.value = directory.lectures.find((row) => row.lecture_id === detail.lecture_id) ?? null
    recoverPlan.value = await getRecoverAttendancePlan(lectureId.value).catch(() => null)
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'This lecture could not be loaded.'
  } finally {
    loading.value = false
  }
}
onMounted(load)
watch(lectureId, load)

const stages = computed(() => lecture.value?.stages)
const qa = computed(() => stages.value?.QA_EVALUATION ?? null)
const perfect = computed(() => stages.value?.PERFECT_ELIGIBILITY ?? null)
const attendanceStage = computed(() => stages.value?.ATTENDANCE ?? null)
const recoverAvailable = computed(() => recoverPlan.value?.available === true)
/** The platform's Recording Link answer for this lecture; never a URL. */
const recording = computed(() => lecture.value?.recording_link ?? null)
const codedRecordings = computed(() => recording.value?.recording_link_mode === 'write')
const momentKey = computed(() => entry.value?.legacy_session_key ?? null)
const momentCount = computed(() => entry.value?.positive_clips_count ?? 0)
const analysed = computed(() => entry.value?.clips_analysis_completeness === 'positive_clips_v5_final')

const tabs = computed(() => [
  { id: 'overview', label: 'Overview' },
  { id: 'pipeline', label: 'Pipeline' },
  { id: 'qa', label: 'Quality' },
  { id: 'moments', label: 'Positive Moments', count: analysed.value ? momentCount.value : null },
])

const versionEntries = computed<[string, string][]>(() => {
  const detail = lecture.value
  if (!detail) return []
  const rows: [string, string][] = [['lecture_id', detail.lecture_id],
    ['orchestration_version', detail.orchestration_version]]
  for (const [key, value] of Object.entries(detail.versions ?? {})) {
    rows.push([key, Array.isArray(value) ? value.join(', ') : String(value)])
  }
  if (detail.duplicate_resolution?.winner_lecture_id) {
    rows.push(['duplicate_winner_lecture_id', detail.duplicate_resolution.winner_lecture_id])
  }
  return rows
})

async function openAction(kind: 'Retry' | 'Recover attendance') {
  actionKind.value = kind
  actionPlan.value = null
  notice.value = ''
  try {
    actionPlan.value = kind === 'Retry'
      ? await getRetryPlan(lectureId.value)
      : await getRecoverAttendancePlan(lectureId.value)
  } catch (caught) {
    actionKind.value = ''
    notice.value = caught instanceof Error ? caught.message : 'The action plan could not be loaded.'
  }
}

async function confirmAction() {
  if (!actionKind.value || !actionPlan.value) return
  actionBusy.value = true
  try {
    if (actionKind.value === 'Retry') {
      await retryLecture(lectureId.value, String(actionPlan.value.action ?? ''))
    } else {
      await recoverAttendance(lectureId.value)
    }
    actionKind.value = ''
    actionPlan.value = null
    await load()
    notice.value = 'The action completed and this lecture has been re-read from persisted state.'
  } catch (caught) {
    notice.value = caught instanceof Error
      ? caught.message
      : 'The guarded action was refused. Nothing was changed.'
  } finally {
    actionBusy.value = false
  }
}
</script>

<template>
  <div class="page stack">
    <RouterLink class="btn-quiet" :to="{ name: 'lectures' }">
      <AppIcon name="chevronLeft" :size="14" /> Lectures
    </RouterLink>

    <LoadingSkeleton v-if="loading" variant="cards" :rows="4" />
    <ErrorState v-else-if="error" :message="error" @retry="load" />

    <template v-else-if="lecture">
      <PageHeader :kicker="entry?.trainer ?? 'Lecture'" :title="lecture.subject">
        <template #meta>
          <div class="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-sm text-muted">
            <span class="inline-flex items-center gap-1.5">
              <AppIcon name="calendar" :size="15" />{{ businessDateLong(lecture.session_date) }}
            </span>
            <span
              v-if="cairoTimeRange(entry?.scheduled_start ?? lecture.scheduled_start, entry?.scheduled_end)"
              class="inline-flex items-center gap-1.5"
            >
              <AppIcon name="clock" :size="15" />
              {{ cairoTimeRange(entry?.scheduled_start ?? lecture.scheduled_start, entry?.scheduled_end) }} Cairo
            </span>
            <span v-if="entry?.trainer" class="inline-flex items-center gap-1.5">
              <AppIcon name="users" :size="15" />{{ entry.trainer }}
            </span>
          </div>
          <div class="mt-3 flex flex-wrap gap-2">
            <StatusBadge :label="bucketLabel(lecture.bucket)" :tone="bucketTone(lecture.bucket)" :title="lecture.bucket" />
            <StatusBadge
              :label="actionLabel(lecture.next_executable_action)"
              :tone="actionTone(lecture.next_executable_action)"
              :title="lecture.next_executable_action"
            />
            <StatusBadge
              v-if="lecture.blocking_stage"
              :label="`At: ${stageLabel(lecture.blocking_stage)}`"
              tone="neutral"
            />
          </div>
        </template>
        <template #actions>
          <button
            v-if="lecture.retry_eligibility.retry_eligible"
            type="button"
            class="btn-secondary"
            @click="openAction('Retry')"
          >
            <AppIcon name="refresh" :size="16" /> Retry
          </button>
          <button
            v-if="recoverAvailable"
            type="button"
            class="btn-primary"
            @click="openAction('Recover attendance')"
          >
            <AppIcon name="users" :size="16" /> Recover attendance
          </button>
        </template>
      </PageHeader>

      <p v-if="notice" class="notice" role="status">
        <AppIcon name="info" :size="16" class="mt-0.5" />{{ notice }}
      </p>

      <div
        v-if="lecture.is_suppressed_duplicate"
        class="notice-warn"
        role="note"
      >
        <AppIcon name="layers" :size="16" class="mt-0.5" />
        <span>
          <strong class="font-semibold">Duplicate calendar event suppressed.</strong>
          The teaching calendar carried a second event for this lecture. It is kept for audit and never
          enters processing.
          <RouterLink
            v-if="lecture.duplicate_resolution?.winner_lecture_id"
            class="font-semibold underline"
            :to="{ name: 'lecture', params: { lectureId: lecture.duplicate_resolution.winner_lecture_id } }"
          >Open the lecture that was kept.</RouterLink>
        </span>
      </div>

      <TabNav :tabs="tabs" :active="tab" @select="selectTab" />

      <!-- ================= Overview ================= -->
      <template v-if="tab === 'overview'">
        <section class="grid gap-3 lg:grid-cols-3">
          <SectionPanel title="What happens next">
            <p class="text-sm leading-6 text-body">{{ actionLabel(lecture.next_action) }}</p>
            <p v-if="lecture.blocking_stage" class="mt-1.5 text-xs text-muted">
              Current step: {{ stageLabel(lecture.blocking_stage) }}
            </p>
            <div class="mt-3 divider" />
            <p class="mt-3 text-xs font-semibold uppercase tracking-kicker text-muted">Retry</p>
            <p class="mt-1 text-sm" :class="lecture.retry_eligibility.retry_eligible ? 'text-emerald-700' : 'text-muted'">
              {{ lecture.retry_eligibility.retry_eligible ? 'Available' : 'Not available' }}
            </p>
            <p v-if="lecture.retry_eligibility.retry_reason" class="mt-0.5 text-xs leading-5 text-muted">
              {{ retryReason(lecture.retry_eligibility.retry_reason) }}
            </p>
          </SectionPanel>

          <SectionPanel title="Attendance">
            <!--
              A suppressed duplicate is not waiting for anything - nothing will
              ever be read for it - so it must not borrow the waiting language.
            -->
            <template v-if="lecture.is_suppressed_duplicate">
              <StatusBadge label="Not applicable" tone="quiet" />
              <p class="mt-3 text-xs leading-5 text-muted">
                Attendance is never resolved for a suppressed duplicate calendar event.
              </p>
            </template>
            <template v-else>
              <AttendanceCell
                v-if="attendanceStage"
                :authoritative="lecture.attendance_source_authoritative"
                :coverage-status="lecture.attendance_coverage_status"
                :attended-count="(attendanceStage.attended_count as number | undefined) ?? null"
              />
              <p class="mt-3 text-xs leading-5 text-muted">
                Attendance is read from an external source. Until that source answers, the platform reports
                waiting rather than a confirmed zero.
              </p>
            </template>
          </SectionPanel>

          <SectionPanel title="Perfect lecture">
            <StatusBadge
              :label="perfectReason(perfect?.reason as string | null).label"
              :tone="perfectReason(perfect?.reason as string | null).tone"
              :title="(perfect?.reason as string | null) ?? undefined"
            />
            <dl class="mt-3 space-y-1.5 text-sm">
              <div class="flex items-center justify-between">
                <dt class="text-muted">Eligibility</dt>
                <dd><StageChip :state="stages!.PERFECT_ELIGIBILITY.state" /></dd>
              </div>
              <div class="flex items-center justify-between">
                <dt class="text-muted">Synced to legacy</dt>
                <dd><StageChip :state="stages!.PERFECT_SYNC.state" /></dd>
              </div>
            </dl>
          </SectionPanel>
        </section>

        <section class="grid gap-3 lg:grid-cols-2">
          <SectionPanel title="Meeting and transcript">
            <dl class="space-y-2 text-sm">
              <div class="flex items-center justify-between gap-4">
                <dt class="text-muted">Teams meeting match</dt><dd><StageChip :state="stages!.MEETING.state" /></dd>
              </div>
              <div class="flex items-center justify-between gap-4">
                <dt class="text-muted">Transcript retrieved</dt><dd><StageChip :state="stages!.TRANSCRIPT.state" /></dd>
              </div>
              <div class="flex items-center justify-between gap-4">
                <dt class="text-muted">Canonical transcript</dt><dd><StageChip :state="stages!.CANONICAL_CUES.state" /></dd>
              </div>
              <div class="flex items-center justify-between gap-4">
                <dt class="text-muted">Speakers attributed</dt><dd><StageChip :state="stages!.SPEAKERS.state" /></dd>
              </div>
              <div class="flex items-center justify-between gap-4">
                <dt class="text-muted">Engagement measured</dt><dd><StageChip :state="stages!.ENGAGEMENT.state" /></dd>
              </div>
            </dl>
          </SectionPanel>

          <SectionPanel title="Recording and reporting">
            <dl class="space-y-2 text-sm">
              <div class="flex items-center justify-between gap-4">
                <dt class="text-muted">Recording link</dt><dd><StageChip :state="stages!.RECORDING_LINK.state" /></dd>
              </div>
              <div class="flex items-center justify-between gap-4">
                <dt class="text-muted">Excel sync</dt><dd><StageChip :state="stages!.EXCEL_SYNC.state" /></dd>
              </div>
              <div class="flex items-center justify-between gap-4">
                <dt class="text-muted">QA sync to legacy</dt><dd><StageChip :state="stages!.LEGACY_QA_SYNC.state" /></dd>
              </div>
            </dl>
            <p v-if="codedRecordings" class="mt-3 text-xs leading-5 text-muted">
              The recording link is made by this platform's recording step, only for an exact, unique Teams
              recording. The Excel stamp is still produced by the legacy workflow.
            </p>
            <p v-else class="mt-3 text-xs leading-5 text-muted">
              The recording link and the Excel stamp are produced by the live legacy workflows, not by this
              platform. Waiting on them does not hold the lecture open.
            </p>
            <a
              v-if="entry?.recording_url"
              class="btn-secondary btn-sm mt-3"
              :href="entry.recording_url"
              target="_blank"
              rel="noopener noreferrer"
            ><AppIcon name="play" :size="14" /> Watch the recording</a>
          </SectionPanel>
        </section>

        <TechnicalDetails :entries="versionEntries" />
      </template>

      <!-- ================= Pipeline ================= -->
      <template v-else-if="tab === 'pipeline'">
        <StageMatrix
          :stage-order="lecture.stage_order"
          :stages="lecture.stages"
          :blocking-stage="lecture.blocking_stage"
        />

        <SectionPanel
          v-if="recording && !lecture.is_suppressed_duplicate"
          title="Recording link"
          :note="codedRecordings
            ? 'Linked automatically by the pipeline, only for one exact recording.'
            : 'Observed only: the legacy n8n recording workflow still makes this link.'"
        >
          <template #actions>
            <StatusBadge
              v-if="recording.state"
              :label="stageState(recording.state).label"
              :tone="stageState(recording.state).tone"
            />
          </template>

          <p v-if="recording.refused_to_guess" class="notice-warn mb-4" role="note">
            <AppIcon name="info" :size="16" class="mt-0.5" />
            <span>
              <strong class="font-semibold">The platform deliberately did not choose a recording.</strong>
              More than one file (or no exactly matching file) fits this lecture, and linking the wrong
              lecture's recording is worse than linking none. A person needs to decide.
            </span>
          </p>

          <dl class="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
            <div class="flex items-center justify-between gap-4">
              <dt class="text-muted">Recording available</dt>
              <dd class="font-semibold" :class="recording.recording_available ? 'text-emerald-700' : 'text-muted'">
                {{ recording.recording_available ? 'Yes' : 'No' }}
              </dd>
            </div>
            <div v-if="recording.owner" class="flex items-center justify-between gap-4">
              <dt class="text-muted">Linked by</dt>
              <dd>{{ recording.recording_available ? recordingOwner(recording.owner) : '—' }}</dd>
            </div>
            <div class="flex items-center justify-between gap-4">
              <dt class="text-muted">Next step</dt>
              <dd>{{ actionLabel(recording.action ?? 'NOTHING_TO_DO') }}</dd>
            </div>
            <div v-if="recording.last_status" class="flex items-center justify-between gap-4">
              <dt class="text-muted">Last result</dt>
              <dd class="font-mono text-2xs" :title="recording.last_reason ?? undefined">{{ recording.last_status }}</dd>
            </div>
            <div v-if="recording.last_checked_at" class="flex items-center justify-between gap-4">
              <dt class="text-muted">Last checked</dt>
              <dd>{{ cairoDateTime(recording.last_checked_at) }}</dd>
            </div>
            <div v-if="recording.attempt_count" class="flex items-center justify-between gap-4">
              <dt class="text-muted">Checks so far</dt>
              <dd class="tabular-nums">{{ recording.attempt_count }}</dd>
            </div>
            <div v-if="recording.next_attempt_after" class="flex items-center justify-between gap-4">
              <dt class="text-muted">Next automatic check</dt>
              <dd>{{ cairoDateTime(recording.next_attempt_after) }}</dd>
            </div>
            <div v-if="recording.source" class="flex items-center justify-between gap-4">
              <dt class="text-muted">Found in</dt>
              <dd>
                {{ recordingSource(recording.source) }}
                <span v-if="recording.timestamp_difference_seconds !== null" class="text-2xs text-faint">
                  · {{ recording.timestamp_difference_seconds.toFixed(1) }} s before Teams
                </span>
              </dd>
            </div>
          </dl>

          <p
            v-if="recording.last_reason || recording.reason"
            class="mt-3 text-xs leading-5 text-muted"
          >
            {{ recording.last_reason ?? humanise(recording.reason) }}
          </p>

          <a
            v-if="recording.recording_available && entry?.recording_url"
            class="btn-secondary btn-sm mt-3"
            :href="entry.recording_url"
            target="_blank"
            rel="noopener noreferrer"
          ><AppIcon name="play" :size="14" /> Watch full lecture</a>
        </SectionPanel>

        <SectionPanel title="Run history" flush>
          <EmptyState
            v-if="!lecture.run_history.length"
            compact
            tone="neutral"
            icon="clock"
            title="This lecture has not appeared in a recorded run"
          />
          <table v-else class="data-table">
            <thead>
              <tr>
                <th scope="col">Action</th>
                <th scope="col">Result</th>
                <th scope="col">From → to</th>
                <th scope="col" class="text-right">When</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(item, index) in lecture.run_history" :key="index">
                <td class="font-semibold text-ink">{{ actionLabel(String(item.action ?? '')) }}</td>
                <td>{{ humanise(String(item.status ?? '')) }}</td>
                <td class="text-muted">
                  {{ humanise(String(item.initial_state ?? '')) }} → {{ humanise(String(item.final_state ?? '')) }}
                </td>
                <td class="whitespace-nowrap text-right text-muted">
                  {{ cairoDateTime(item.started_at as string | null) }}
                </td>
              </tr>
            </tbody>
          </table>
        </SectionPanel>
      </template>

      <!-- ================= Quality ================= -->
      <template v-else-if="tab === 'qa'">
        <section class="grid gap-3 lg:grid-cols-3">
          <SectionPanel title="Checklist outcome">
            <div v-if="qa && qa.met_count !== undefined" class="space-y-2">
              <div class="flex items-center justify-between">
                <span class="text-sm text-muted">Met</span>
                <span class="text-lg font-bold tabular-nums text-emerald-700">{{ qa.met_count }}</span>
              </div>
              <div class="flex items-center justify-between">
                <span class="text-sm text-muted">Partial</span>
                <span class="text-lg font-bold tabular-nums text-amber-700">{{ qa.partial_count }}</span>
              </div>
              <div class="flex items-center justify-between">
                <span class="text-sm text-muted">Not met</span>
                <span class="text-lg font-bold tabular-nums text-rose-700">{{ qa.not_met_count }}</span>
              </div>
            </div>
            <p v-else class="text-sm text-muted">The checklist has not been evaluated for this lecture yet.</p>
          </SectionPanel>

          <SectionPanel title="Quality pipeline">
            <dl class="space-y-2 text-sm">
              <div class="flex items-center justify-between gap-4">
                <dt class="text-muted">Evaluation</dt><dd><StageChip :state="stages!.QA_EVALUATION.state" /></dd>
              </div>
              <div class="flex items-center justify-between gap-4">
                <dt class="text-muted">Report rendered</dt><dd><StageChip :state="stages!.QA_RENDER.state" /></dd>
              </div>
              <div class="flex items-center justify-between gap-4">
                <dt class="text-muted">Written to legacy record</dt><dd><StageChip :state="stages!.LEGACY_QA_SYNC.state" /></dd>
              </div>
            </dl>
            <p v-if="qa?.qa_status" class="mt-3 text-xs text-muted">
              Platform status: {{ humanise(String(qa.qa_status)) }}
            </p>
          </SectionPanel>

          <SectionPanel title="Perfect policy">
            <StatusBadge
              :label="perfectReason(perfect?.reason as string | null).label"
              :tone="perfectReason(perfect?.reason as string | null).tone"
            />
            <p class="mt-3 text-xs leading-5 text-muted">
              Pending attendance is not a failure — the policy simply cannot be applied until the attendance
              source has answered.
            </p>
            <p class="mt-2 text-2xs text-faint">
              Policy {{ lecture.versions.perfect_eligibility_version }}
            </p>
          </SectionPanel>
        </section>

        <p class="text-xs text-muted">
          Model prompts and raw model responses are deliberately not exposed in this console.
        </p>
      </template>

      <!-- ================= Positive Moments ================= -->
      <template v-else>
        <SectionPanel title="Positive moments">
          <EmptyState
            v-if="!analysed"
            compact
            tone="neutral"
            icon="moments"
            title="No positive-moment analysis for this lecture yet"
            message="Positive-moment analysis runs separately from the processing pipeline. When it completes for this lecture, its moments will be linked here."
          />
          <template v-else>
            <p class="text-sm text-body">
              The analysis found
              <strong class="font-semibold text-ink">{{ momentCount }}</strong>
              positive moment{{ momentCount === 1 ? '' : 's' }} in this lecture on
              {{ businessDate(lecture.session_date) }}.
            </p>
            <RouterLink
              v-if="momentKey"
              class="btn-primary mt-4"
              :to="{ name: 'positive-moment-detail', params: { sessionKey: momentKey } }"
            >
              <AppIcon name="moments" :size="16" /> Open the moments
            </RouterLink>
          </template>
        </SectionPanel>
      </template>

      <ActionPlanDialog
        :open="Boolean(actionKind)"
        :title="actionKind"
        :plan="actionPlan"
        :busy="actionBusy"
        @close="actionKind = ''"
        @confirm="confirmAction"
      />
    </template>
  </div>
</template>
