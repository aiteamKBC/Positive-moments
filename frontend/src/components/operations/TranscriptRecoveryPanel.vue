<script setup lang="ts">
/**
 * Manual Transcript Recovery: every lecture, on any date, whose Teams
 * transcript content Microsoft Graph never delivered and that an upload could
 * complete now.
 *
 * The list is the server's (GET transcript-recovery/), confirmed lecture by
 * lecture with the same rule the upload dialog itself applies, so nothing here
 * offers an upload the server would refuse. "Upload" hands the lecture to the
 * parent, which opens the one TranscriptUploadDialog. This panel writes nothing.
 *
 * Below the list: the background processing jobs those uploads queued (Queued,
 * Running, Completed, Failed), re-read every 20 s while any is still active.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import AppIcon from '../AppIcon.vue'
import ProcessingJobStatus from './ProcessingJobStatus.vue'
import { getTranscriptRecoveryQueue } from '../../services/operations'
import type { ProcessingJob, TranscriptRecoveryLecture } from '../../types/operations'
import { businessDateLong, cairoTimeRange } from '../../utils/datetime'
import { humanise } from '../../utils/labels'

const props = defineProps<{
  open: boolean
  /** Bumped by the parent after an upload changed something, to reload the list. */
  refreshKey?: number
  /** The lecture whose upload is being opened, so its button can say so. */
  openingLectureId?: string | null
}>()
const emit = defineEmits<{ close: []; upload: [lectureId: string] }>()

const lectures = ref<TranscriptRecoveryLecture[]>([])
const jobs = ref<ProcessingJob[]>([])
const activeJobs = computed(() => jobs.value.some((job) => job.status === 'QUEUED' || job.status === 'RUNNING'))
const POLL_MS = 20_000
let timer: ReturnType<typeof setTimeout> | null = null
const truncated = ref(false)
const loading = ref(false)
const error = ref('')

async function load(quiet = false) {
  if (!quiet) loading.value = true
  if (!quiet) error.value = ''
  try {
    const queue = await getTranscriptRecoveryQueue()
    lectures.value = queue.lectures
    truncated.value = queue.truncated
    jobs.value = queue.jobs ?? []
  } catch (caught) {
    if (!quiet) error.value = caught instanceof Error ? caught.message : 'The recovery list could not be loaded.'
  } finally {
    loading.value = false
    schedule()
  }
}

function schedule() {
  if (timer) clearTimeout(timer)
  timer = null
  if (props.open && activeJobs.value) timer = setTimeout(() => load(true), POLL_MS)
}

watch(() => props.open, (open) => { if (open) load(); else schedule() }, { immediate: true })
onBeforeUnmount(() => { if (timer) clearTimeout(timer) })
watch(() => props.refreshKey, () => { if (props.open) load() })

function statusOf(row: TranscriptRecoveryLecture) {
  const reason = row.missing_parts.map((part) => part.graph_failure_reason).find(Boolean)
  return reason ? `Graph: ${humanise(reason)}` : 'Content not retrieved from Microsoft Graph'
}

function partsOf(row: TranscriptRecoveryLecture) {
  return row.part_count > 1
    ? `${row.missing_part_count} of ${row.part_count} transcript parts missing`
    : 'Teams transcript listed, content missing'
}
</script>

<template>
  <div
    v-if="open"
    class="fixed inset-0 z-50 grid place-items-center bg-brand-950/60 p-4 backdrop-blur-sm"
    role="presentation"
    @click.self="emit('close')"
  >
    <section
      class="surface max-h-[90vh] w-full max-w-3xl overflow-y-auto"
      role="dialog"
      aria-modal="true"
      aria-label="Manual Transcript Recovery"
      data-test="transcript-recovery-panel"
    >
      <div class="panel-head">
        <div>
          <p class="kicker">Operations</p>
          <h2 class="mt-1 text-lg font-bold text-ink">Manual Transcript Recovery</h2>
          <p class="mt-1 text-sm text-muted">
            Lectures on any date whose Teams transcript Microsoft Graph has not delivered. Upload the
            .vtt downloaded from Teams; the file is checked against the lecture before anything is stored.
          </p>
        </div>
        <button type="button" class="icon-btn" aria-label="Close" @click="emit('close')">
          <AppIcon name="close" :size="16" />
        </button>
      </div>

      <div class="panel-body space-y-3">
        <p v-if="loading" class="text-sm text-muted" data-test="recovery-loading">Loading lectures…</p>
        <div v-else-if="error" class="notice" role="alert" data-test="recovery-error">
          <AppIcon name="alert" :size="16" class="mt-0.5" />
          <span>{{ error }}</span>
          <button type="button" class="btn-quiet ml-auto" @click="load()">Try again</button>
        </div>
        <p v-else-if="!lectures.length" class="text-sm text-muted" data-test="recovery-empty">
          No lecture is missing transcript content. Nothing needs a manual upload.
        </p>
        <table v-else class="data-table" data-test="recovery-table">
          <thead>
            <tr>
              <th scope="col">Lecture</th>
              <th scope="col">Session date</th>
              <th scope="col">Transcript status</th>
              <th scope="col"><span class="sr-only">Action</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in lectures" :key="row.lecture_id" :data-test="`recovery-row-${row.lecture_id}`">
              <td class="max-w-xs">
                <RouterLink class="row-link" :to="{ name: 'lecture', params: { lectureId: row.lecture_id } }">
                  {{ row.subject }}
                </RouterLink>
                <p v-if="row.module" class="text-xs text-muted">{{ row.module }}</p>
              </td>
              <td class="whitespace-nowrap">
                {{ businessDateLong(row.session_date) }}
                <p class="text-xs text-muted">{{ cairoTimeRange(row.scheduled_start, row.scheduled_end) }}</p>
              </td>
              <td>
                <p class="text-sm font-semibold text-amber-700">{{ partsOf(row) }}</p>
                <p class="text-xs text-muted">{{ statusOf(row) }}</p>
                <p v-if="row.previous_upload_count" class="text-xs text-muted">
                  {{ row.previous_upload_count }} earlier manual upload{{ row.previous_upload_count === 1 ? '' : 's' }}
                </p>
              </td>
              <td class="text-right">
                <button
                  type="button"
                  class="btn-primary whitespace-nowrap"
                  :disabled="Boolean(openingLectureId)"
                  :data-test="`recovery-upload-${row.lecture_id}`"
                  @click="emit('upload', row.lecture_id)"
                >
                  <AppIcon name="upload" :size="16" />
                  {{ openingLectureId === row.lecture_id ? 'Opening…' : 'Upload transcript' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
        <section v-if="jobs.length" class="pt-2" data-test="recovery-jobs">
          <h3 class="eyebrow mb-2">Background processing</h3>
          <table class="data-table">
            <thead>
              <tr>
                <th scope="col">Lecture</th>
                <th scope="col">Session date</th>
                <th scope="col">Processing</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="job in jobs" :key="job.job_id" :data-test="`recovery-job-${job.lecture_id}`">
                <td class="max-w-xs">
                  <RouterLink class="row-link" :to="{ name: 'lecture', params: { lectureId: job.lecture_id } }">
                    {{ job.subject }}
                  </RouterLink>
                </td>
                <td class="whitespace-nowrap">{{ businessDateLong(job.session_date) }}</td>
                <td><ProcessingJobStatus compact :job="job" /></td>
              </tr>
            </tbody>
          </table>
        </section>
        <p v-if="truncated" class="text-xs text-muted">
          Showing the most recent {{ lectures.length }} lectures; older ones appear as these are recovered.
        </p>
      </div>
    </section>
  </div>
</template>
