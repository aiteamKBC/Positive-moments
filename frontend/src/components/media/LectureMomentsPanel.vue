<script setup lang="ts">
/**
 * Lecture Detail → Positive Moments / Media.
 *
 * Analysis, recording and every moment with its clip. All actions queue
 * background work (or change one job's state) and return at once; the panel
 * polls while anything is in flight.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import AppIcon from '../AppIcon.vue'
import EmptyState from '../EmptyState.vue'
import ErrorState from '../ErrorState.vue'
import LoadingSkeleton from '../LoadingSkeleton.vue'
import SectionPanel from '../SectionPanel.vue'
import StatusBadge from '../StatusBadge.vue'
import MomentCard from './MomentCard.vue'
import {
  getLecturePositiveMoments, openAsset, openRecording, queueMediaRun, renderMoment, retryMoment,
} from '../../services/media'
import type { LectureMediaDetail } from '../../types/media'
import { cairoDateTime } from '../../utils/datetime'
import { errorMessage, openSafely } from '../../utils/format'
import { analysisState, clock, humanise, mediaRecordingState } from '../../utils/labels'

const props = defineProps<{ lectureId: string; sessionDate: string }>()

const detail = ref<LectureMediaDetail | null>(null)
const loading = ref(true)
const error = ref('')
const notice = ref('')
const busy = ref(false)
let poll: ReturnType<typeof setInterval> | undefined

const IN_FLIGHT = ['QUEUED', 'RENDERING', 'RENDER_SUCCEEDED', 'UPLOAD_PENDING', 'UPLOADING']
const inFlight = computed(() => (detail.value?.moments ?? [])
  .some(m => m.job && IN_FLIGHT.includes(m.job.status)))
const recording = computed(() => detail.value?.recording ?? null)
const analysis = computed(() => detail.value?.analysis ?? null)
const readyCount = computed(() => detail.value?.totals.ready_to_render ?? 0)

async function load(quiet = false) {
  if (!quiet) loading.value = true
  try {
    detail.value = await getLecturePositiveMoments(props.lectureId)
    error.value = ''
  } catch (caught) {
    if (!quiet) error.value = errorMessage(caught, 'Positive Moments could not be loaded.')
  } finally {
    loading.value = false
  }
}

function schedule() {
  if (poll) clearInterval(poll)
  poll = setInterval(() => { if (inFlight.value) load(true) }, 10_000)
}

onMounted(() => { load(); schedule() })
onBeforeUnmount(() => { if (poll) clearInterval(poll) })
watch(() => props.lectureId, () => load())

async function act(label: string, work: () => Promise<unknown>) {
  busy.value = true
  notice.value = ''
  try {
    await work()
    notice.value = label
    await load(true)
  } catch (caught) {
    notice.value = errorMessage(caught, 'The action was refused. Nothing was changed.')
  } finally {
    busy.value = false
  }
}

const analyze = () => act('Analysis queued. The media runner will process this lecture in the background.',
  () => queueMediaRun('ANALYZE', props.sessionDate, props.sessionDate, props.lectureId))
const renderAll = () => act('Rendering queued for every safe clip of this lecture.',
  () => queueMediaRun('RENDER', props.sessionDate, props.sessionDate, props.lectureId))
const render = (momentId: string) => act('Clip queued for rendering.', () => renderMoment(momentId))
const retry = (momentId: string) => act('Retry scheduled.', () => retryMoment(momentId))

async function openClip(assetId: string) {
  try { openSafely(await openAsset(assetId)) } catch (caught) {
    notice.value = errorMessage(caught, 'The clip link is not available.')
  }
}
async function openFull() {
  try { openSafely(await openRecording(props.lectureId)) } catch (caught) {
    notice.value = errorMessage(caught, 'This lecture has no recording that can be opened yet.')
  }
}
</script>

<template>
  <div class="stack">
    <LoadingSkeleton v-if="loading" variant="cards" :rows="3" />
    <ErrorState v-else-if="error" :message="error" @retry="load()" />
    <template v-else-if="detail">
      <p v-if="notice" class="notice" role="status"><AppIcon name="info" :size="16" class="mt-0.5" />{{ notice }}</p>

      <section class="grid gap-3 lg:grid-cols-2">
        <SectionPanel title="Analysis">
          <template #actions>
            <StatusBadge v-bind="analysisState(detail.state?.analysis_state)" />
          </template>
          <dl v-if="analysis" class="space-y-1.5 text-sm">
            <div class="flex justify-between gap-3"><dt class="text-muted">Moments</dt><dd class="tabular-nums">{{ analysis.accepted_count }}</dd></div>
            <div class="flex justify-between gap-3"><dt class="text-muted">Source</dt><dd>{{ analysis.source === 'legacy_v5_import' ? 'Imported from V5 (proven)' : 'Evidence Intelligence' }}</dd></div>
            <div class="flex justify-between gap-3"><dt class="text-muted">Candidates → valid → accepted</dt><dd class="tabular-nums">{{ analysis.candidate_count }} → {{ analysis.structurally_valid_count }} → {{ analysis.accepted_count }}</dd></div>
            <div class="flex justify-between gap-3"><dt class="text-muted">Analyzed</dt><dd>{{ cairoDateTime(analysis.analyzed_at) }}</dd></div>
            <div class="flex justify-between gap-3"><dt class="text-muted">Transcript</dt><dd class="mono text-2xs">{{ analysis.transcript_fingerprint }}</dd></div>
            <div class="flex justify-between gap-3"><dt class="text-muted">Policy</dt><dd class="mono text-2xs break-all">{{ analysis.policy_version }}</dd></div>
          </dl>
          <p v-else class="text-sm text-muted">This lecture has not been analyzed for Positive Moments yet.</p>
          <p v-if="analysis && !analysis.is_current" class="notice-warn mt-3">
            <AppIcon name="info" :size="16" class="mt-0.5" />
            The transcript or policy changed since this analysis. Re-analyze to refresh it.
          </p>
          <button
            v-if="!analysis || !analysis.is_current || analysis.status === 'FAILED'"
            type="button" class="btn-secondary btn-sm mt-3" :disabled="busy" @click="analyze"
          >
            <AppIcon name="sparkle" :size="14" /> {{ analysis ? 'Re-analyze' : 'Analyze' }}
          </button>
        </SectionPanel>

        <SectionPanel title="Recording">
          <template #actions>
            <StatusBadge v-bind="mediaRecordingState(recording?.recording_state)" />
          </template>
          <dl v-if="recording" class="space-y-1.5 text-sm">
            <div class="flex justify-between gap-3"><dt class="text-muted">Status</dt><dd>{{ humanise(recording.recording_status) }}</dd></div>
            <div v-if="recording.recording_file_name" class="flex justify-between gap-3"><dt class="text-muted">File</dt><dd class="min-w-0 truncate" :title="recording.recording_file_name">{{ recording.recording_file_name }}</dd></div>
            <div v-if="recording.recording_duration_seconds" class="flex justify-between gap-3"><dt class="text-muted">Duration</dt><dd class="tabular-nums">{{ clock(recording.recording_duration_seconds) }}</dd></div>
            <div v-if="recording.recording_rule" class="flex justify-between gap-3"><dt class="text-muted">Chosen because</dt><dd>{{ humanise(recording.recording_rule) }}</dd></div>
            <div class="flex justify-between gap-3"><dt class="text-muted">Last checked</dt><dd>{{ cairoDateTime(recording.recording_checked_at) }}</dd></div>
          </dl>
          <p v-if="recording?.recording_state !== 'READY' && recording?.recording_reason" class="mt-3 text-xs leading-5 text-muted">
            {{ recording.recording_reason }}
          </p>
          <p v-if="!recording" class="text-sm text-muted">Run a Preview from Positive Moments Media to check this lecture's recording.</p>
          <button v-if="recording?.recording_state === 'READY'" type="button" class="btn-secondary btn-sm mt-3" @click="openFull">
            <AppIcon name="play" :size="14" /> Open full recording
          </button>
        </SectionPanel>
      </section>

      <SectionPanel title="Positive Moments" :count="detail.moments.length">
        <template #actions>
          <button v-if="readyCount" type="button" class="btn-primary btn-sm" :disabled="busy" @click="renderAll">
            <AppIcon name="video" :size="14" /> Render {{ readyCount }} safe clip{{ readyCount === 1 ? '' : 's' }}
          </button>
        </template>
        <EmptyState
          v-if="!detail.moments.length"
          compact tone="neutral" icon="moments"
          :title="detail.state?.analysis_state === 'NO_POSITIVE_MOMENTS' ? 'No positive moments in this lecture' : 'No moments yet'"
          :message="detail.state?.analysis_state === 'NO_POSITIVE_MOMENTS'
            ? 'The analysis found no learner-positive evidence that passed verification.'
            : 'Analyze this lecture to find learner-positive evidence.'"
        />
        <div v-else class="grid gap-3">
          <MomentCard
            v-for="moment in detail.moments"
            :key="moment.moment_id"
            :moment="moment"
            :busy="busy"
            @render="render"
            @retry="retry"
            @open-clip="openClip"
            @open-recording="openFull"
          />
        </div>
      </SectionPanel>
    </template>
  </div>
</template>
