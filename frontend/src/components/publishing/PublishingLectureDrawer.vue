<script setup lang="ts">
/**
 * One lecture in full, in a side drawer: identity and LMS mapping, Teams recording, Bunny parts,
 * transcript and captions, release and playback, jobs with their retries, and the timeline.
 *
 * Read-only by design: there is no action here. Operator actions stay in the audited CLI / API.
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import AppIcon from '../AppIcon.vue'
import ErrorState from '../ErrorState.vue'
import LoadingSkeleton from '../LoadingSkeleton.vue'
import StatusBadge from '../StatusBadge.vue'
import { getPublishingLecture } from '../../services/publishing'
import type { PublishingDetail } from '../../types/publishing'
import { businessDate, cairoDateTime, cairoTimeRange, sinceNow } from '../../utils/datetime'
import { errorMessage } from '../../utils/format'
import { humanise } from '../../utils/labels'
import { label } from '../../utils/publishing'

const props = defineProps<{ lectureId: string }>()
const emit = defineEmits<{ close: [] }>()

const detail = ref<PublishingDetail | null>(null)
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    detail.value = await getPublishingLecture(props.lectureId)
  } catch (caught) {
    error.value = errorMessage(caught, 'This lecture could not be loaded.')
  } finally {
    loading.value = false
  }
}

function onKey(event: { key: string }) {
  if (event.key === 'Escape') emit('close')
}

onMounted(() => { load(); window.addEventListener('keydown', onKey) })
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
watch(() => props.lectureId, load)

function minutes(seconds?: number | null) {
  return seconds ? `${Math.round(seconds / 60)} min` : '—'
}
function value(v: unknown) {
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(v)) return cairoDateTime(v)
  return String(v)
}
</script>

<template>
  <div class="fixed inset-0 z-50 flex justify-end bg-brand-950/40 backdrop-blur-[1px]" @click.self="emit('close')">
    <aside
      class="flex h-full w-full max-w-3xl flex-col overflow-hidden bg-canvas shadow-raised"
      role="dialog" aria-modal="true" aria-label="Lecture publishing detail"
    >
      <header class="flex items-start justify-between gap-3 border-b border-line bg-white px-5 py-4">
        <div class="min-w-0">
          <p class="kicker">Lecture publishing</p>
          <h2 class="mt-1 truncate text-lg font-bold text-ink">{{ detail?.row.subject ?? 'Lecture' }}</h2>
          <p v-if="detail" class="mt-0.5 text-sm text-muted">
            {{ businessDate(detail.row.session_date) }} · {{ cairoTimeRange(detail.row.scheduled_start, detail.row.scheduled_end) }}
            <template v-if="detail.row.module"> · {{ detail.row.module }}</template>
          </p>
        </div>
        <button type="button" class="grid h-9 w-9 place-items-center rounded-lg text-muted hover:bg-brand-50" aria-label="Close" @click="emit('close')">
          <AppIcon name="close" :size="18" />
        </button>
      </header>

      <div class="flex-1 space-y-4 overflow-y-auto px-5 py-4">
        <LoadingSkeleton v-if="loading" variant="table" :rows="5" />
        <ErrorState v-else-if="error" :message="error" @retry="load" />

        <template v-else-if="detail">
          <section class="surface px-4 py-3">
            <div class="flex flex-wrap items-center gap-2">
              <StatusBadge v-bind="label.state(detail.row.state)" />
              <span v-if="detail.row.automatic" class="text-xs text-muted">The publisher continues automatically.</span>
              <span v-else-if="detail.row.needs_attention" class="text-xs font-semibold text-orange-700">An operator needs to act.</span>
            </div>
            <p class="mt-2 text-sm text-ink">{{ detail.row.reason }}</p>
            <p v-if="detail.row.retry_at" class="mt-1 text-xs text-muted">Next automatic check: {{ cairoDateTime(detail.row.retry_at) }}</p>
          </section>

          <section class="surface px-4 py-3">
            <h3 class="section-title">LMS delivery</h3>
            <dl class="mt-2 grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
              <div><dt class="tile-label">Release</dt><dd><StatusBadge v-bind="label.release(detail.row.release.status)" /></dd></div>
              <div>
                <dt class="tile-label">LMS playback</dt>
                <dd>
                  <StatusBadge :label="detail.row.playback.available ? 'Available' : 'Not available'" :tone="detail.row.playback.available ? 'ok' : 'quiet'" />
                  <p v-if="!detail.row.playback.available && detail.row.playback.reason" class="mt-0.5 text-2xs text-muted">{{ humanise(detail.row.playback.reason) }}</p>
                </dd>
              </div>
              <div><dt class="tile-label">Released at</dt><dd>{{ value(detail.delivery?.released_at) }}</dd></div>
              <div><dt class="tile-label">Withdrawn at</dt><dd>{{ value(detail.delivery?.withdrawn_at) }}</dd></div>
              <div><dt class="tile-label">Delivery state</dt><dd>{{ humanise(detail.delivery?.state ?? 'NO_VIDEO') }}</dd></div>
              <div><dt class="tile-label">Bunny library</dt><dd class="tabular-nums">{{ detail.library_id }}</dd></div>
            </dl>
            <p v-if="detail.delivery?.blocking.length" class="mt-2 text-xs text-muted">
              Blocking: <span class="font-mono">{{ detail.delivery.blocking.join(', ') }}</span>
            </p>
          </section>

          <section class="surface px-4 py-3">
            <h3 class="section-title">Identity and LMS mapping</h3>
            <dl class="mt-2 grid grid-cols-1 gap-y-2 text-sm sm:grid-cols-2">
              <div><dt class="tile-label">Lecture id</dt><dd class="break-all font-mono text-xs">{{ detail.row.lecture_id }}</dd></div>
              <div>
                <dt class="tile-label">LMS match</dt>
                <dd>
                  <StatusBadge v-bind="label.lms(detail.lms.match?.status ?? 'PENDING')" />
                  <span v-if="detail.lms.match?.rule_version" class="ml-1 text-2xs text-faint">rule {{ detail.lms.match.rule_version }}</span>
                </dd>
              </div>
            </dl>
            <div v-if="detail.lms.links.length" class="table-wrap mt-3 !shadow-none">
              <table class="data-table">
                <thead><tr><th>LMS occurrence</th><th>Live session</th><th>Role</th><th>Current</th></tr></thead>
                <tbody>
                  <tr v-for="link in detail.lms.links" :key="link.occurrence_id + link.link_role">
                    <td class="font-mono text-xs">{{ link.occurrence_id }}</td>
                    <td class="font-mono text-xs">{{ link.live_session_id ?? '—' }}</td>
                    <td>{{ humanise(link.link_role) }}</td>
                    <td>{{ link.is_current ? 'Yes' : 'No' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p v-else class="mt-2 text-sm text-muted">No LMS occurrence is linked to this lecture.</p>
          </section>

          <section class="surface px-4 py-3">
            <h3 class="section-title">Teams recording</h3>
            <dl v-if="detail.recording.discovery" class="mt-2 grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
              <div><dt class="tile-label">Status</dt><dd><StatusBadge v-bind="label.recording(String(detail.recording.discovery.status))" /></dd></div>
              <div><dt class="tile-label">Reason</dt><dd class="font-mono text-xs">{{ value(detail.recording.discovery.reason_code) }}</dd></div>
              <div><dt class="tile-label">Parts located</dt><dd>{{ value(detail.recording.discovery.located_part_count) }} / {{ value(detail.recording.discovery.expected_part_count) }}</dd></div>
              <div><dt class="tile-label">Checks</dt><dd>{{ value(detail.recording.discovery.attempt_count) }}</dd></div>
              <div><dt class="tile-label">Last checked</dt><dd>{{ value(detail.recording.discovery.last_checked_at) }}</dd></div>
              <div><dt class="tile-label">Next check</dt><dd>{{ value(detail.recording.discovery.next_check_after) }}</dd></div>
              <div v-if="detail.recording.discovery.review_reason" class="col-span-full">
                <dt class="tile-label">Review reason</dt><dd class="font-mono text-xs">{{ detail.recording.discovery.review_reason }}</dd>
              </div>
            </dl>
            <p v-else class="mt-2 text-sm text-muted">The publisher has not looked for this lecture's recording yet.</p>
            <ul v-if="detail.recording.parts.length" class="mt-3 space-y-1 text-sm">
              <li v-for="part in detail.recording.parts" :key="part.part_index" class="flex flex-wrap items-center gap-2">
                <span class="font-semibold">Part {{ part.part_index }}</span>
                <StatusBadge :label="humanise(part.locate_status)" :tone="part.locate_status === 'LOCATED' ? 'ok' : 'waiting'" />
                <span class="text-muted">{{ humanise(part.classification ?? 'UNKNOWN') }} · {{ minutes(part.duration_seconds) }}</span>
                <span class="text-2xs text-faint">{{ part.transcript_paired ? 'transcript paired' : 'no transcript paired' }}</span>
              </li>
            </ul>
          </section>

          <section class="surface px-4 py-3">
            <h3 class="section-title">Bunny video, transcript and captions</h3>
            <div class="mt-2 flex flex-wrap gap-3 text-sm">
              <span>Import <StatusBadge v-bind="label.bunny(detail.row.bunny.status)" /></span>
              <span>Video <StatusBadge v-bind="label.video(detail.row.video.status)" /></span>
              <span>Transcript <StatusBadge v-bind="label.transcript(detail.transcript.status)" /></span>
              <span>Captions <StatusBadge v-bind="label.captions(detail.row.captions.status)" /></span>
            </div>
            <p class="mt-1 text-2xs text-faint">Captions are optional: a lecture plays without them, and a caption produced later is added to the same video.</p>
            <div v-if="detail.delivery?.parts.length" class="table-wrap mt-3 !shadow-none">
              <table class="data-table">
                <thead><tr><th>Part</th><th>Bunny video id</th><th>Encoding</th><th>Visibility</th><th>Resolutions</th><th>Captions</th></tr></thead>
                <tbody>
                  <tr v-for="part in detail.delivery.parts" :key="part.source_part_index">
                    <td>{{ part.sequence }}<span class="text-2xs text-faint"> (Teams {{ part.source_part_index }})</span></td>
                    <td class="break-all font-mono text-xs">{{ part.bunny?.video_id ?? '—' }}</td>
                    <td><StatusBadge v-bind="label.video(part.encoding === 'NOT_IMPORTED' ? 'NOT_STARTED' : part.encoding)" /></td>
                    <td>{{ humanise(part.visibility ?? 'NONE') }}</td>
                    <td class="text-xs">{{ part.resolutions.join(', ') || '—' }}</td>
                    <td class="text-xs">
                      <template v-if="part.captions.length">
                        <span v-for="caption in part.captions" :key="caption.language">
                          {{ caption.language }}: {{ humanise(caption.status) }}{{ caption.content_verified ? ' ✓' : ' (not read back)' }}
                        </span>
                      </template>
                      <template v-else>None</template>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p v-else class="mt-2 text-sm text-muted">No video has been imported into library {{ detail.library_id }} yet.</p>
          </section>

          <section class="surface px-4 py-3">
            <h3 class="section-title">Jobs and retries <span class="font-normal text-faint">{{ detail.jobs.length }}</span></h3>
            <div v-if="detail.jobs.length" class="table-wrap mt-2 !shadow-none">
              <table class="data-table">
                <thead><tr><th>Job</th><th>Status</th><th>Attempts</th><th>Next attempt</th><th>Last error</th><th>Updated</th></tr></thead>
                <tbody>
                  <tr v-for="job in detail.jobs" :key="job.job_id">
                    <td>{{ humanise(job.job_kind) }}<p class="font-mono text-2xs text-faint">{{ job.subject_key }}</p></td>
                    <td><StatusBadge :value="job.status" :tone="job.status === 'SUCCEEDED' ? 'ok' : job.status === 'FAILED' ? 'error' : job.status === 'REVIEW_REQUIRED' ? 'review' : job.status === 'CANCELLED' ? 'quiet' : 'waiting'" /></td>
                    <td class="tabular-nums">{{ job.attempt_count }} / {{ job.max_attempts }}</td>
                    <td class="whitespace-nowrap">{{ value(job.next_attempt_at) }}</td>
                    <td class="font-mono text-xs">{{ job.last_error_code ?? '—' }}</td>
                    <td class="whitespace-nowrap text-muted">{{ sinceNow(job.updated_at, '—') }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p v-else class="mt-2 text-sm text-muted">No publisher job exists for this lecture yet.</p>
          </section>

          <section class="surface px-4 py-3">
            <h3 class="section-title">Timeline <span class="font-normal text-faint">latest {{ detail.timeline.length }}</span></h3>
            <ol v-if="detail.timeline.length" class="mt-2 space-y-1.5 text-sm">
              <li v-for="event in detail.timeline" :key="event.event_id" class="flex flex-wrap items-baseline gap-x-2">
                <span class="whitespace-nowrap text-xs text-muted">{{ cairoDateTime(event.at) }}</span>
                <span class="font-semibold">{{ humanise(event.event) }}</span>
                <span class="text-2xs text-faint">{{ event.actor }}</span>
              </li>
            </ol>
            <p v-else class="mt-2 text-sm text-muted">No publisher events yet.</p>
          </section>
        </template>
      </div>
    </aside>
  </div>
</template>
