<script setup lang="ts">
/**
 * Upload a Teams transcript (.vtt) for ONE lecture, when Microsoft Graph will
 * not serve it.
 *
 * Choose -> the SERVER previews the file against this lecture (nothing stored)
 * -> the operator confirms the lecture and the file -> store -> process.
 *
 * Every verdict on this page is the server's: which Teams transcript part the
 * file attaches to, whether it is valid, what is wrong with it. The page
 * formats those answers and decides none of them. Confirming sends back the
 * file's SHA-256 and the part the preview showed, so a different file or a plan
 * that changed meanwhile is refused rather than stored.
 *
 * Storing also QUEUES the lecture's processing, in the same transaction. The
 * pipeline - the paid quality evaluation included - runs in the background
 * worker, never in a request: the window can be closed as soon as the file is
 * stored, and "process" here only (re)queues.
 */
import { computed, ref, watch } from 'vue'

import AppIcon from '../AppIcon.vue'
import {
  previewTranscriptUpload, processTranscriptUpload, storeTranscriptUpload,
} from '../../services/operations'
import type {
  ProcessingPlan, ProcessingQueueResult, TranscriptPart, TranscriptUploadPreview, TranscriptUploadStatus,
  UploadProblem,
} from '../../types/operations'
import { businessDateLong, cairoDateTime, cairoTimeRange } from '../../utils/datetime'
import { actionLabel, humanise } from '../../utils/labels'

type Step = 'choose' | 'checking' | 'review' | 'storing' | 'process' | 'processing' | 'done'

const props = defineProps<{
  open: boolean
  lectureId: string
  status: TranscriptUploadStatus | null
  /** Open straight at "process the uploaded transcript" rather than at choosing a file. */
  processOnly?: boolean
}>()
const emit = defineEmits<{ close: []; changed: [] }>()

const step = ref<Step>('choose')
const file = ref<File | null>(null)
const partId = ref('')
const preview = ref<TranscriptUploadPreview | null>(null)
const problems = ref<UploadProblem[]>([])
/** A problem with the chosen file, found before sending it. */
const error = ref('')
/** The part still has to be chosen. Kept apart from `error`: choosing a part clears it. */
const partError = ref('')
watch(partId, () => { partError.value = '' })
const stored = ref(false)
const plan = ref<ProcessingPlan | null>(null)
const outcome = ref<{ tone: 'ok' | 'warn' | 'error'; title: string; detail: string } | null>(null)

const missingParts = computed<TranscriptPart[]>(() => (props.status?.parts ?? []).filter((part) => !part.has_content))
const needsPartChoice = computed(() => missingParts.value.length > 1)
const busy = computed(() => ['checking', 'storing', 'processing'].includes(step.value))
const lecture = computed(() => preview.value?.lecture ?? props.status?.lecture ?? null)

watch(() => props.open, (open) => {
  if (!open) return
  file.value = null
  partId.value = ''
  preview.value = null
  problems.value = []
  error.value = ''
  partError.value = ''
  stored.value = false
  outcome.value = null
  plan.value = props.status?.processing ?? null
  step.value = props.processOnly ? 'process' : 'choose'
}, { immediate: true })

function close() {
  if (!busy.value) emit('close')
}

/** The server's refusal, as the list of problems it named. */
function serverProblems(caught: unknown): UploadProblem[] {
  const data = (caught as { response?: { data?: { problems?: UploadProblem[]; code?: string; detail?: string } } })
    ?.response?.data
  if (data?.problems?.length) return data.problems
  if (data?.detail) return [{ code: data.code ?? 'ERROR', message: data.detail }]
  return [{ code: 'NETWORK', message: caught instanceof Error ? caught.message : 'The request failed.' }]
}

function onFile(event: Event) {
  const chosen = (event.target as HTMLInputElement).files?.[0] ?? null
  file.value = chosen
  error.value = ''
  if (!chosen) return
  const limits = props.status?.limits
  if (!chosen.name.toLowerCase().endsWith(limits?.extension ?? '.vtt')) {
    error.value = 'Choose the .vtt transcript file downloaded from Microsoft Teams.'
  } else if (limits && chosen.size > limits.max_bytes) {
    error.value = `The file is larger than ${Math.round(limits.max_bytes / 1024 / 1024)} MB; a Teams transcript is far smaller.`
  }
}

async function check() {
  if (!file.value || error.value) return
  if (needsPartChoice.value && !partId.value) {
    partError.value = 'Choose which transcript part this file is.'
    return
  }
  step.value = 'checking'
  problems.value = []
  try {
    preview.value = await previewTranscriptUpload(props.lectureId, file.value, partId.value || undefined)
    step.value = 'review'
  } catch (caught) {
    problems.value = serverProblems(caught)
    step.value = 'choose'
  }
}

async function confirm() {
  const current = preview.value
  if (!file.value || !current?.ok || !current.part || !current.file) return
  step.value = 'storing'
  problems.value = []
  try {
    const result = await storeTranscriptUpload(props.lectureId, file.value, current.part.artifact_id,
      current.file.content_sha256)
    stored.value = true
    emit('changed')
    outcome.value = queuedOutcome(result.queue ?? null,
      result.status === 'ALREADY_STORED' ? 'This transcript was already stored' : 'Transcript stored')
    step.value = 'done'
  } catch (caught) {
    problems.value = serverProblems(caught)
    step.value = 'review'
  }
}

/**
 * What the server's queue answered, in words. The pipeline (quality evaluation included) runs in the
 * background worker, never in this request, so there is nothing to wait for here.
 */
function queuedOutcome(queue: ProcessingQueueResult | null, stored: string) {
  if (queue?.queued) {
    return {
      tone: 'ok' as const,
      title: queue.created ? `${stored} — processing queued` : `${stored} — processing already queued`,
      detail: 'This lecture is processed in the background, quality evaluation included. You can close this '
        + 'window; the lecture page shows the job as Queued, Running, Completed or Failed.',
    }
  }
  const waiting = queue?.reason === 'WAITING_FOR_TRANSCRIPT_PARTS' || queue?.reason === 'QUEUE_UNAVAILABLE'
  return {
    tone: waiting ? 'warn' as const : 'ok' as const,
    title: stored,
    detail: queue?.reason_message ?? 'Nothing else is waiting on this transcript.',
  }
}

/** Queue (or re-queue) this lecture's processing. Returns at once; the worker does the work. */
async function queueProcessing() {
  step.value = 'processing'
  outcome.value = null
  try {
    const queue = await processTranscriptUpload(props.lectureId)
    outcome.value = queuedOutcome(queue, 'Transcript stored')
  } catch (caught) {
    const [first] = serverProblems(caught)
    outcome.value = {
      tone: 'error',
      title: 'Processing was not queued',
      detail: `${first.message} The transcript stays stored.`,
    }
  }
  emit('changed')
  step.value = 'done'
}

function duration(ms: number | null | undefined) {
  if (ms === null || ms === undefined) return '—'
  const minutes = Math.round(ms / 60000)
  return minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : `${minutes} min`
}

function size(bytes: number | null | undefined) {
  if (!bytes) return '—'
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`
}

const COST_LABELS: Record<string, string> = {
  calls_microsoft_graph: 'Calls Microsoft Graph',
  buys_model_generation: 'Buys a model generation (quality evaluation)',
  writes_legacy_production_row: 'Writes the legacy QA record',
  affects_other_lectures_that_day: 'Affects other lectures that day',
}
const cost = computed(() => Object.entries(plan.value?.cost ?? {})
  .filter(([key]) => key in COST_LABELS)
  .map(([key, flag]) => ({ key, label: COST_LABELS[key], yes: Boolean(flag) })))
</script>

<template>
  <div
    v-if="open"
    class="fixed inset-0 z-50 grid place-items-center bg-brand-950/60 p-4 backdrop-blur-sm"
    role="presentation"
    @click.self="close"
  >
    <section
      class="surface max-h-[90vh] w-full max-w-xl overflow-y-auto"
      role="dialog"
      aria-modal="true"
      aria-label="Upload transcript"
      data-test="transcript-upload-dialog"
    >
      <div class="panel-head">
        <div>
          <p class="kicker">Manual transcript</p>
          <h2 class="mt-1 text-lg font-bold text-ink">
            {{ processOnly ? 'Process the uploaded transcript' : 'Upload transcript' }}
          </h2>
        </div>
        <button type="button" class="icon-btn" aria-label="Close" :disabled="busy" @click="close">
          <AppIcon name="close" :size="16" />
        </button>
      </div>

      <div class="panel-body space-y-4">
        <!-- The lecture this file is for. Always shown: it is what the operator confirms. -->
        <div v-if="lecture" class="rounded-lg border border-line px-3 py-2.5" data-test="upload-target-lecture">
          <p class="eyebrow">Lecture</p>
          <p class="mt-0.5 text-sm font-semibold text-ink">{{ lecture.subject }}</p>
          <p class="text-xs text-muted">
            {{ businessDateLong(lecture.session_date) }}
            <template v-if="cairoTimeRange(lecture.scheduled_start, lecture.scheduled_end)">
              · {{ cairoTimeRange(lecture.scheduled_start, lecture.scheduled_end) }} Cairo
            </template>
          </p>
        </div>

        <!-- ---------- choose ---------- -->
        <template v-if="step === 'choose' || step === 'checking'">
          <p class="text-sm leading-6 text-muted">
            Use this when Microsoft Graph could not retrieve this lecture's Teams transcript. Download the
            transcript from the meeting in Teams as <strong>.vtt</strong> and choose it here. Nothing is stored
            until you have checked the preview and confirmed.
          </p>

          <div v-if="needsPartChoice">
            <label class="eyebrow" for="transcript-part">Transcript part</label>
            <select id="transcript-part" v-model="partId" class="field mt-1" data-test="upload-part">
              <option value="" disabled>Choose the part this file is</option>
              <option v-for="part in missingParts" :key="part.artifact_id" :value="part.artifact_id">
                Part {{ part.part_index }} · Teams {{ cairoDateTime(part.teams_started_at) }} · {{ duration(part.duration_ms) }}
              </option>
            </select>
          </div>

          <div>
            <label class="eyebrow" for="transcript-file">Teams transcript file</label>
            <input
              id="transcript-file"
              type="file"
              accept=".vtt,text/vtt"
              class="mt-1 block w-full text-sm"
              data-test="upload-file"
              :disabled="busy"
              @change="onFile"
            />
          </div>
        </template>

        <!-- ---------- review (the server's preview) ---------- -->
        <template v-if="step === 'review' || step === 'storing'">
          <div v-if="preview?.part" class="rounded-lg border border-line px-3 py-2.5" data-test="upload-target-part">
            <p class="eyebrow">Teams transcript it will be stored on</p>
            <p class="mt-0.5 text-sm text-body">
              Part {{ preview.part.part_index }} of {{ preview.parts.length }}
              · Teams {{ cairoDateTime(preview.part.teams_started_at) }} · {{ duration(preview.part.duration_ms) }}
            </p>
            <p v-if="preview.part.graph_failure_reason" class="text-xs text-muted">
              Graph retrieval: {{ humanise(preview.part.graph_failure_reason) }}
            </p>
          </div>

          <dl v-if="preview?.file" class="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm" data-test="upload-file-facts">
            <dt class="text-muted">File</dt><dd class="truncate text-ink" :title="preview.file.filename">{{ preview.file.filename }}</dd>
            <dt class="text-muted">Size</dt><dd>{{ size(preview.file.content_bytes) }}</dd>
            <dt class="text-muted">Cues</dt><dd class="tabular-nums">{{ preview.file.cue_count }} ({{ preview.file.spoken_cue_count }} with speech)</dd>
            <dt class="text-muted">Covers</dt><dd>{{ duration(preview.file.last_cue_end_ms) }}</dd>
            <dt class="text-muted">Speakers named</dt><dd class="tabular-nums">{{ preview.file.speaker_label_count }}</dd>
            <dt class="text-muted">SHA-256</dt><dd class="font-mono text-2xs">{{ preview.file.content_sha256.slice(0, 16) }}…</dd>
          </dl>

          <p v-if="preview?.duplicate" class="notice" role="status">
            <AppIcon name="info" :size="16" class="mt-0.5" />
            This exact file is already stored for this transcript part. Confirming changes nothing.
          </p>

          <ul v-if="preview?.errors.length" class="space-y-1.5" role="alert" data-test="upload-errors">
            <li v-for="item in preview.errors" :key="item.code" class="flex gap-2 text-sm text-rose-700">
              <AppIcon name="alert" :size="15" class="mt-0.5 shrink-0" />{{ item.message }}
            </li>
          </ul>
          <ul v-if="preview?.warnings.length" class="space-y-1.5" data-test="upload-warnings">
            <li v-for="item in preview.warnings" :key="item.code" class="flex gap-2 text-sm text-amber-700">
              <AppIcon name="info" :size="15" class="mt-0.5 shrink-0" />{{ item.message }}
            </li>
          </ul>
          <p v-if="preview?.ok" class="text-xs leading-5 text-muted">
            Confirming stores this file as the lecture's transcript and queues this lecture's processing in the
            background: canonical transcript, speakers, attendance, engagement and the quality evaluation. You do not
            need to keep this window open. Other lectures are not touched. A stored transcript is never replaced by an
            upload.
          </p>
        </template>

        <!-- ---------- process an already stored upload ---------- -->
        <template v-if="step === 'process'">
          <p class="text-sm leading-6 text-muted">
            Queue this lecture for background processing from its earliest incomplete step, using the uploaded
            transcript, quality evaluation included. Steps that are already complete are not redone. Only this lecture
            is processed, and the window can be closed once it is queued.
          </p>
          <p v-if="plan && !plan.available" class="notice-warn" role="note">
            <AppIcon name="info" :size="16" class="mt-0.5" />{{ plan.reason_message ?? humanise(plan.reason ?? '') }}
          </p>
          <p v-else-if="plan" class="text-sm text-body">Next step: <strong>{{ actionLabel(plan.next_action) }}</strong></p>
          <ul v-if="cost.length" class="space-y-1.5">
            <li v-for="item in cost" :key="item.key" class="flex items-center gap-2 text-sm">
              <AppIcon :name="item.yes ? 'alert' : 'check'" :size="15" :class="item.yes ? 'text-amber-600' : 'text-emerald-600'" />
              <span :class="item.yes ? 'text-ink' : 'text-muted'">{{ item.label }} — <strong>{{ item.yes ? 'yes' : 'no' }}</strong></span>
            </li>
          </ul>
        </template>

        <!-- ---------- progress ---------- -->
        <p v-if="step === 'checking'" class="notice" role="status" data-test="upload-progress">
          <AppIcon name="clock" :size="16" class="mt-0.5" />Checking the file against this lecture…
        </p>
        <p v-if="step === 'storing'" class="notice" role="status" data-test="upload-progress">
          <AppIcon name="clock" :size="16" class="mt-0.5" />Storing the transcript…
        </p>
        <p v-if="step === 'processing'" class="notice" role="status" data-test="upload-progress">
          <AppIcon name="clock" :size="16" class="mt-0.5" />Queuing this lecture's processing…
        </p>

        <!-- ---------- outcome ---------- -->
        <div
          v-if="step === 'done' && outcome"
          :class="outcome.tone === 'error' ? 'notice-error' : outcome.tone === 'warn' ? 'notice-warn' : 'notice'"
          role="status"
          data-test="upload-outcome"
        >
          <AppIcon :name="outcome.tone === 'error' ? 'alert' : 'checkCircle'" :size="16" class="mt-0.5" />
          <span><strong class="font-semibold">{{ outcome.title }}.</strong> {{ outcome.detail }}</span>
        </div>

        <p v-if="error || partError" class="text-sm text-rose-700" role="alert" data-test="upload-client-error">
          {{ error || partError }}
        </p>
        <ul v-if="problems.length" class="space-y-1.5" role="alert" data-test="upload-server-errors">
          <li v-for="item in problems" :key="item.code" class="flex gap-2 text-sm text-rose-700">
            <AppIcon name="alert" :size="15" class="mt-0.5 shrink-0" />{{ item.message }}
          </li>
        </ul>
      </div>

      <div class="flex justify-end gap-2 border-t border-line px-5 py-3">
        <template v-if="step === 'choose' || step === 'checking'">
          <button type="button" class="btn-secondary" :disabled="busy" @click="close">Cancel</button>
          <button
            type="button"
            class="btn-primary"
            data-test="upload-check"
            :disabled="!file || Boolean(error) || busy"
            @click="check"
          >
            {{ step === 'checking' ? 'Checking…' : 'Check file' }}
          </button>
        </template>
        <template v-else-if="step === 'review' || step === 'storing'">
          <button type="button" class="btn-secondary" :disabled="busy" @click="step = 'choose'">Back</button>
          <button
            type="button"
            class="btn-primary"
            data-test="upload-confirm"
            :disabled="!preview?.ok || busy"
            @click="confirm"
          >
            {{ step === 'storing' ? 'Storing…' : 'Confirm upload' }}
          </button>
        </template>
        <template v-else-if="step === 'process'">
          <button type="button" class="btn-secondary" @click="close">Cancel</button>
          <button
            type="button"
            class="btn-primary"
            data-test="upload-process"
            :disabled="!plan?.available"
            @click="queueProcessing"
          >
            Queue processing
          </button>
        </template>
        <template v-else-if="step === 'processing'">
          <button type="button" class="btn-primary" disabled>Queuing…</button>
        </template>
        <template v-else>
          <button
            v-if="outcome?.tone === 'error'"
            type="button"
            class="btn-secondary"
            data-test="upload-resume"
            @click="queueProcessing"
          >
            Try again
          </button>
          <button type="button" class="btn-primary" @click="emit('close')">Close</button>
        </template>
      </div>
    </section>
  </div>
</template>
