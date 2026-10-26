<script setup lang="ts">
/**
 * The publisher's alerts, exactly as the reconciler stored them (bunny_publisher_alerts), with the
 * signed-in user's own notification state.
 *
 * Opening an alert marks it READ for this user; DISMISS (with a reason) moves it to history for this
 * user. Neither changes the publisher's alert: only the reconciler resolves it, when its condition is
 * no longer observed. There is no acknowledge / resolve / retry / release / delete action here.
 *
 * Alerts are grouped by their STORED severity only: DIGEST is the informational daily digest (its
 * counters folded away until asked for, never unread); every other severity is listed. A lecture, job
 * or LMS session is shown only when the alert names one. Nothing is inferred.
 */
import { computed, reactive, ref, watch } from 'vue'

import AppIcon from '../AppIcon.vue'
import EmptyState from '../EmptyState.vue'
import SectionPanel from '../SectionPanel.vue'
import StatusBadge from '../StatusBadge.vue'
import { dismissAlert, markAlertRead, restoreAlert } from '../../services/publishing'
import type { AlertReview, PublisherAlert, PublisherAlerts } from '../../types/publishing'
import { businessDate, cairoDateTime, sinceNow } from '../../utils/datetime'
import { errorMessage } from '../../utils/format'
import { humanise } from '../../utils/labels'
import { ALERT_VIEWS, type AlertView, DIGEST, inAlertView, label, reviewBadge } from '../../utils/publishing'

const props = defineProps<{ data: PublisherAlerts | null; error?: string }>()
const emit = defineEmits<{ close: []; openLecture: [lectureId: string]; changed: [] }>()

/** Review answers received since the last load (the list reloads after every change). */
const reviews = reactive<Record<string, AlertReview>>({})
/** Alerts opened in the current view: they stay listed after they become read, until the view changes. */
const pinned = reactive(new Set<string>())
const expanded = reactive(new Set<string>())
const view = ref<AlertView | null>(null)
const actionError = ref('')
const busy = ref('')
const dismissing = ref<string | null>(null)
const reason = ref('')
const note = ref('')

watch(() => props.data, (data) => {
  for (const key of Object.keys(reviews)) delete reviews[key]
  // Open on what needs attention: the new alerts, or the read ones when nothing is new.
  if (data && view.value === null) view.value = data.review_counts.new ? 'new' : 'read'
}, { immediate: true })
watch(view, () => pinned.clear())

const reviewOf = (alert: PublisherAlert) => reviews[alert.alert_id] ?? alert.review
const everything = computed(() => [...(props.data?.alerts ?? []), ...(props.data?.history ?? [])])
const listed = computed(() => everything.value.filter(alert =>
  alert.severity !== DIGEST && view.value !== null
  && (inAlertView(reviewOf(alert).state, view.value) || pinned.has(alert.alert_id))))
const digests = computed(() => (props.data?.alerts ?? []).filter(alert => alert.severity === DIGEST))
const viewCount = (key: AlertView) => everything.value.filter(alert => inAlertView(reviewOf(alert).state, key)).length
const newCount = computed(() => viewCount('new'))

async function toggle(alert: PublisherAlert) {
  if (expanded.has(alert.alert_id)) {
    expanded.delete(alert.alert_id)
    return
  }
  expanded.add(alert.alert_id)
  pinned.add(alert.alert_id)
  if (alert.severity !== DIGEST && reviewOf(alert).state === 'NEW') await act(alert, () => markAlertRead(alert.alert_id))
}

async function act(alert: PublisherAlert, call: () => Promise<{ review: AlertReview }>) {
  busy.value = alert.alert_id
  actionError.value = ''
  try {
    reviews[alert.alert_id] = (await call()).review
    emit('changed')
    return true
  } catch (caught) {
    actionError.value = errorMessage(caught, 'Your review of this alert could not be saved.')
    return false
  } finally {
    busy.value = ''
  }
}

function startDismiss(alert: PublisherAlert) {
  dismissing.value = alert.alert_id
  reason.value = ''
  note.value = ''
}

const noteRequired = computed(() => reason.value === 'OTHER')
const canDismiss = computed(() => !!reason.value && (!noteRequired.value || !!note.value.trim()) && note.value.length <= 300)

async function confirmDismiss(alert: PublisherAlert) {
  if (await act(alert, () => dismissAlert(alert.alert_id, reason.value, note.value.trim()))) {
    pinned.add(alert.alert_id)
    dismissing.value = null
  }
}

const reasonLabel = (code: string | null) =>
  props.data?.dismiss_reasons.find(r => r.code === code)?.label ?? humanise(code)

const LMS_SOURCE_MISSING: Record<string, string> = {
  NOT_CONFIGURED: 'The LMS database is not configured for this dashboard, so the LMS title is not shown.',
  UNAVAILABLE: 'The LMS database could not be read just now.',
  NOT_FOUND: 'The LMS no longer has this occurrence.',
}

function detailValue(value: unknown) {
  if (value === null || value === undefined || value === '') return '—'
  if (Array.isArray(value)) return value.join(', ')
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
</script>

<template>
  <SectionPanel
    title="Publisher alerts"
    :count="data ? `${newCount} new` : null"
    note="As recorded by the publisher's reconciliation. Reading or dismissing an alert is your own review: only the publisher resolves an alert, when its condition is no longer observed."
    flush
  >
    <template #actions>
      <button type="button" class="btn-quiet btn-sm" @click="emit('close')">
        <AppIcon name="close" :size="14" /> Hide
      </button>
    </template>
    <p v-if="error && !data" class="notice-error m-4">{{ error }}</p>
    <div v-else-if="!data" class="p-4"><div class="skeleton h-16 w-full" /></div>
    <template v-else>
      <div class="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2.5">
        <div class="inline-flex rounded-lg border border-line bg-white p-0.5" role="group" aria-label="Alert views">
          <button
            v-for="option in ALERT_VIEWS" :key="option.key" type="button"
            class="rounded-md px-3 py-1.5 text-[0.8125rem] font-semibold transition"
            :class="view === option.key ? 'bg-brand-700 text-white' : 'text-muted hover:text-brand-700'"
            :aria-pressed="view === option.key" :data-test="`view-${option.key}`" @click="view = option.key"
          >
            {{ option.label }} <span class="ml-0.5 tabular-nums opacity-80">{{ viewCount(option.key) }}</span>
          </button>
        </div>
        <span v-if="view === 'history'" class="text-2xs text-muted">Resolved alerts from the last {{ data.history_days }} days, and those you dismissed.</span>
      </div>
      <p v-if="actionError" class="notice-error mx-4 mt-3" role="alert">{{ actionError }}</p>
      <p v-if="error" class="notice-warn mx-4 mt-3" role="status">{{ error }} Showing the last alerts received.</p>

      <EmptyState
        v-if="!listed.length" compact
        :title="view === 'new' ? 'No new alerts' : view === 'read' ? 'No read alerts open' : view === 'history' ? 'No history' : 'No alerts'"
        :message="view === 'new' ? 'Everything open has been read or dismissed.' : 'Nothing to show in this view.'"
        data-test="alerts-empty"
      />
      <ul v-else class="divide-y divide-line" data-test="alert-list">
        <li v-for="alert in listed" :key="alert.alert_id" data-test="alert-row">
          <button
            type="button" class="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-left hover:bg-canvas"
            :aria-expanded="expanded.has(alert.alert_id)" :aria-controls="`alert-${alert.alert_id}`"
            data-test="alert-open" @click="toggle(alert)"
          >
            <StatusBadge v-bind="label.alert(alert.severity)" />
            <StatusBadge v-if="reviewOf(alert).state !== 'READ'" v-bind="reviewBadge(reviewOf(alert).state)" data-test="alert-state" />
            <span class="min-w-0 flex-1 text-sm">
              <span class="font-semibold text-ink" :class="reviewOf(alert).state === 'NEW' ? '' : 'font-medium'">{{ humanise(alert.kind) }}</span>
              <span class="ml-1.5 text-xs text-muted">{{ alert.summary }}</span>
              <span v-if="alert.lms?.source?.title" class="ml-1.5 text-xs font-semibold text-body">· {{ alert.lms.source.title }}</span>
              <span v-else-if="alert.lecture?.subject" class="ml-1.5 text-xs font-semibold text-body">· {{ alert.lecture.subject }}</span>
            </span>
            <span class="whitespace-nowrap text-xs text-muted" :title="cairoDateTime(alert.last_seen_at)">{{ sinceNow(alert.last_seen_at, '—') }}</span>
            <AppIcon name="chevronDown" :size="14" class="text-faint transition" :class="expanded.has(alert.alert_id) ? 'rotate-180' : ''" />
          </button>

          <div v-show="expanded.has(alert.alert_id)" :id="`alert-${alert.alert_id}`" class="space-y-3 px-4 pb-4" data-test="alert-details">
            <!-- Lecture / job the alert names -->
            <div v-if="alert.lecture || alert.job_id" class="flex flex-wrap items-baseline gap-x-3 text-sm">
              <button
                v-if="alert.lecture" type="button" class="row-link text-left font-semibold"
                data-test="alert-lecture" @click="emit('openLecture', alert.lecture.lecture_id)"
              >
                {{ alert.lecture.subject ?? 'Lecture' }}<template v-if="alert.lecture.session_date">, {{ businessDate(alert.lecture.session_date) }}</template>
              </button>
              <span v-if="alert.job_id" class="font-mono text-2xs text-muted">job {{ alert.job_id }}</span>
            </div>

            <!-- The LMS session the alert names -->
            <section v-if="alert.lms" class="rounded-lg border border-line p-3" data-test="alert-lms" aria-label="LMS session">
              <h3 class="text-xs font-semibold uppercase tracking-wide text-muted">LMS session</h3>
              <p class="mt-1 text-sm font-semibold text-ink" data-test="lms-title">
                <template v-if="alert.lms.source?.title">{{ alert.lms.source.title }}</template>
                <span v-else class="font-normal text-muted">Title not available</span>
                <span v-if="alert.lms.source?.session_number" class="ml-1.5 text-xs font-normal text-muted">
                  session {{ alert.lms.source.session_number }}<template v-if="alert.lms.source.series_sessions"> of {{ alert.lms.source.series_sessions }}</template>
                </span>
              </p>
              <p v-if="alert.lms.source && alert.lms.source.status !== 'OK'" class="text-2xs text-muted" data-test="lms-source-missing">
                {{ LMS_SOURCE_MISSING[alert.lms.source.status] ?? humanise(alert.lms.source.status) }}
              </p>
              <dl class="mt-2 grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
                <div class="flex gap-2"><dt class="text-faint">Programme</dt><dd class="text-ink">{{ alert.lms.source?.programme ?? alert.lms.programme ?? '—' }}</dd></div>
                <div v-if="alert.lms.source?.group || alert.lms.source?.cohort" class="flex gap-2">
                  <dt class="text-faint">Group</dt><dd class="text-ink">{{ [alert.lms.source?.group, alert.lms.source?.cohort].filter(Boolean).join(' · ') }}</dd>
                </div>
                <div class="flex gap-2"><dt class="text-faint">Scheduled</dt><dd class="text-ink">{{ cairoDateTime(alert.lms.scheduled_start) }} – {{ cairoDateTime(alert.lms.scheduled_end) }}</dd></div>
                <div class="flex gap-2"><dt class="text-faint">Occurrence</dt><dd class="font-mono text-ink">{{ alert.lms.occurrence_id }}</dd></div>
                <div v-if="alert.lms.series_id" class="flex gap-2"><dt class="text-faint">Series</dt><dd class="font-mono text-ink">{{ alert.lms.series_id }}</dd></div>
                <div class="flex gap-2"><dt class="text-faint">LMS status</dt><dd class="text-ink">{{ humanise(alert.lms.source?.occurrence_status ?? alert.lms.occurrence_status) }} · series {{ humanise(alert.lms.series_status) }} · {{ humanise(alert.lms.meeting_type) }}</dd></div>
                <template v-if="alert.lms.source?.status === 'OK'">
                  <div class="flex gap-2" data-test="lms-took-place">
                    <dt class="text-faint">Took place</dt>
                    <dd class="text-ink">
                      <template v-if="alert.lms.source.actual_start">started {{ cairoDateTime(alert.lms.source.actual_start) }}</template>
                      <template v-else>No actual start recorded in the LMS</template>
                      · {{ alert.lms.source.participant_count ?? 0 }} participants
                      · {{ alert.lms.source.attendance_report ? 'attendance report present' : 'no attendance report' }}
                    </dd>
                  </div>
                </template>
                <div class="flex gap-2"><dt class="text-faint">Teams meeting</dt><dd class="text-ink">{{ alert.lms.has_teams_meeting ? 'Linked in the LMS' : 'None in the LMS' }}</dd></div>
              </dl>
              <p class="mt-2 text-xs" data-test="lms-coverage">
                <span class="font-semibold text-ink">{{ humanise(alert.lms.coverage_status) }}</span>
                <span class="text-muted"> ({{ humanise(alert.lms.scope_status) }}): {{ alert.lms.coverage_meaning ?? '—' }}</span>
                <span v-if="typeof alert.lms.evidence.hours_since_end === 'number'" class="text-muted"> Ended {{ Math.round(alert.lms.evidence.hours_since_end) }} h before the last check.</span>
              </p>
            </section>

            <!-- The named lecture's LMS match evaluation -->
            <p v-if="alert.match" class="text-xs" data-test="alert-match">
              <span class="text-faint">LMS match</span>
              <span class="ml-1.5 font-semibold text-ink">{{ humanise(alert.match.status) }}</span>
              <span v-if="alert.match.reason" class="text-muted"> — {{ alert.match.reason }}</span>
              <span v-if="alert.match.candidates !== null" class="text-muted"> · {{ alert.match.candidates }} LMS candidate{{ alert.match.candidates === 1 ? '' : 's' }}</span>
            </p>

            <dl v-if="Object.keys(alert.detail).length" class="grid gap-x-6 gap-y-0.5 text-2xs sm:grid-cols-2" data-test="alert-stored-detail">
              <div v-for="(value, key) in alert.detail" :key="key" class="flex gap-1.5">
                <dt class="shrink-0 text-faint">{{ humanise(String(key)) }}</dt>
                <dd class="break-all font-mono text-muted">{{ detailValue(value) }}</dd>
              </div>
            </dl>

            <p class="text-2xs text-muted">
              Seen {{ alert.occurrences }}× · first {{ cairoDateTime(alert.first_seen_at) }} · last {{ cairoDateTime(alert.last_seen_at) }}
              <template v-if="alert.resolved_at"> · resolved by the publisher {{ cairoDateTime(alert.resolved_at) }}</template>
              <template v-if="alert.notification_status"> · notification {{ humanise(alert.notification_status).toLowerCase() }}</template>
            </p>

            <!-- Your review -->
            <div class="flex flex-wrap items-center gap-2 border-t border-line pt-3 text-xs" data-test="alert-review">
              <span class="text-muted">
                <template v-if="reviewOf(alert).dismissed_at">
                  You dismissed this {{ cairoDateTime(reviewOf(alert).dismissed_at) }}: <span class="font-semibold text-ink">{{ reasonLabel(reviewOf(alert).dismiss_reason) }}</span>
                  <template v-if="reviewOf(alert).dismiss_note"> — {{ reviewOf(alert).dismiss_note }}</template>
                </template>
                <template v-else-if="reviewOf(alert).read_at">You read this {{ cairoDateTime(reviewOf(alert).read_at) }}.</template>
              </span>
              <span class="ml-auto flex gap-2">
                <button
                  v-if="reviewOf(alert).dismissed_at" type="button" class="btn-secondary btn-sm" :disabled="busy === alert.alert_id"
                  data-test="alert-restore" @click="act(alert, () => restoreAlert(alert.alert_id))"
                >
                  Restore
                </button>
                <button
                  v-else-if="alert.status === 'OPEN' && dismissing !== alert.alert_id" type="button" class="btn-secondary btn-sm"
                  :disabled="busy === alert.alert_id" data-test="alert-dismiss" @click="startDismiss(alert)"
                >
                  Dismiss…
                </button>
              </span>
            </div>

            <form
              v-if="dismissing === alert.alert_id" class="space-y-2 rounded-lg bg-canvas p-3" data-test="dismiss-form"
              @submit.prevent="confirmDismiss(alert)"
            >
              <fieldset>
                <legend class="text-xs font-semibold text-ink">Why dismiss it? It stays in your history and does not resolve the issue.</legend>
                <div class="mt-1.5 flex flex-wrap gap-x-4 gap-y-1">
                  <label v-for="option in data.dismiss_reasons" :key="option.code" class="flex items-center gap-1.5 text-xs text-body">
                    <input v-model="reason" type="radio" name="dismiss-reason" :value="option.code" :data-test="`reason-${option.code}`">
                    {{ option.label }}
                  </label>
                </div>
              </fieldset>
              <label class="block text-xs text-muted" :for="`note-${alert.alert_id}`">
                Note {{ noteRequired ? '(required)' : '(optional)' }}
              </label>
              <textarea
                :id="`note-${alert.alert_id}`" v-model="note" maxlength="300" rows="2"
                class="w-full rounded-lg border border-line bg-white p-2 text-xs text-ink focus:border-brand-500"
                data-test="dismiss-note"
              />
              <div class="flex justify-end gap-2">
                <button type="button" class="btn-quiet btn-sm" @click="dismissing = null">Cancel</button>
                <button type="submit" class="btn-primary btn-sm" :disabled="!canDismiss || busy === alert.alert_id" data-test="dismiss-confirm">
                  Dismiss for me
                </button>
              </div>
            </form>
          </div>
        </li>
      </ul>

      <!-- The daily digest: informational, never unread, one line each, counters on request -->
      <section v-if="digests.length && view !== 'history'" class="border-t border-line" aria-labelledby="publisher-digest-title" data-test="digest-section">
        <h3 id="publisher-digest-title" class="px-4 pt-3 text-xs font-semibold uppercase tracking-wide text-muted">
          Daily digest <span class="font-normal normal-case tracking-normal text-faint">· informational, stored with severity Digest</span>
        </h3>
        <ul class="divide-y divide-line">
          <li v-for="digest in digests" :key="digest.alert_id" class="px-4 py-2.5" data-test="digest-row">
            <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
              <StatusBadge v-bind="label.alert(digest.severity)" />
              <p class="min-w-0 flex-1 text-sm">
                <span class="font-semibold text-ink">{{ humanise(digest.kind) }}</span>
                <span class="ml-1.5 text-xs text-muted">{{ digest.summary }}</span>
              </p>
              <span class="whitespace-nowrap text-xs text-muted" :title="cairoDateTime(digest.last_seen_at)">{{ sinceNow(digest.last_seen_at, '—') }}</span>
              <button
                type="button" class="btn-quiet btn-sm"
                :aria-expanded="expanded.has(digest.alert_id)" :aria-controls="`digest-${digest.alert_id}`"
                data-test="digest-toggle" @click="toggle(digest)"
              >
                {{ expanded.has(digest.alert_id) ? 'Hide details' : 'View details' }}
              </button>
            </div>
            <div v-show="expanded.has(digest.alert_id)" :id="`digest-${digest.alert_id}`" class="mt-2 rounded-lg bg-canvas p-3" data-test="digest-details">
              <dl class="grid grid-cols-2 gap-x-4 gap-y-1 text-2xs sm:grid-cols-3 lg:grid-cols-4">
                <div v-for="(value, key) in digest.detail" :key="key" class="flex justify-between gap-2">
                  <dt class="text-faint">{{ humanise(String(key)) }}</dt>
                  <dd class="font-mono tabular-nums text-ink">{{ detailValue(value) }}</dd>
                </div>
              </dl>
              <p class="mt-2 text-2xs text-muted">
                Seen {{ digest.occurrences }}× · first {{ cairoDateTime(digest.first_seen_at) }} · last {{ cairoDateTime(digest.last_seen_at) }}
                <template v-if="digest.notification_status"> · notification {{ humanise(digest.notification_status).toLowerCase() }}</template>
              </p>
            </div>
          </li>
        </ul>
      </section>
    </template>
  </SectionPanel>
</template>
