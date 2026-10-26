<script setup lang="ts">
/**
 * Lecture Publishing Monitor (read-only).
 *
 *   Teams -> recording discovery -> LMS match -> Bunny import -> encoding verification
 *         -> automatic release -> LMS delivery API
 *
 * Every number is a count of LECTURES whose Africa/Cairo date is in the range; each KPI card is also
 * the filter for the rows behind it (the backend uses one predicate for both). Health is read from
 * fresh heartbeats: an old record is shown as "Not running", never as healthy, and a health read that
 * fails is shown as unavailable - never green.
 *
 * Captions are optional for playback: a playable lecture without captions is "Playable · no
 * captions", never a failure. There are no actions on this page.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppIcon from '../components/AppIcon.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import FilterBar from '../components/FilterBar.vue'
import LoadingSkeleton from '../components/LoadingSkeleton.vue'
import PageHeader from '../components/PageHeader.vue'
import SectionPanel from '../components/SectionPanel.vue'
import StatusBadge from '../components/StatusBadge.vue'
import PublisherAlertsPanel from '../components/publishing/PublisherAlertsPanel.vue'
import PublishingLectureDrawer from '../components/publishing/PublishingLectureDrawer.vue'
import { getPublisherAlerts, getPublisherHealth, getPublishingLectures, getPublishingSummary } from '../services/publishing'
import type { PublisherAlerts, PublisherHealth, PublishingFilters, PublishingPage, PublishingSummary } from '../types/publishing'
import { businessDate, cairoDateTime, cairoTimeRange, shiftBusinessDate, sinceNow } from '../utils/datetime'
import { errorMessage } from '../utils/format'
import { age, CAPTIONS, label, MONITOR_STATE, newAlertCounts, TRANSCRIPT } from '../utils/publishing'

const REFRESH_MS = 60_000
const PAGE_SIZE = 25
const MAX_DAYS = 31

const route = useRoute()
const router = useRouter()
const text = (name: string) => (typeof route.query[name] === 'string' ? (route.query[name] as string) : '')

function cairoToday(): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Africa/Cairo' }).format(new Date())
}

type Preset = 'today' | 'yesterday' | 'last7' | 'custom'
const preset = ref<Preset>((['today', 'yesterday', 'last7', 'custom'] as const).find(p => p === text('range')) ?? 'today')
const customFrom = ref(text('from') || cairoToday())
const customTo = ref(text('to') || cairoToday())

const range = computed(() => {
  const today = cairoToday()
  if (preset.value === 'yesterday') return { from: shiftBusinessDate(today, -1), to: shiftBusinessDate(today, -1) }
  if (preset.value === 'last7') return { from: shiftBusinessDate(today, -6), to: today }
  if (preset.value === 'custom') return { from: customFrom.value, to: customTo.value }
  return { from: today, to: today }
})
const rangeProblem = computed(() => {
  const { from, to } = range.value
  if (!from || !to) return 'Choose both dates.'
  if (to < from) return 'The end date is before the start date.'
  if ((Date.parse(to) - Date.parse(from)) / 86_400_000 + 1 > MAX_DAYS) return `Choose at most ${MAX_DAYS} days.`
  return ''
})

const filters = ref<PublishingFilters>({
  q: text('q'), kpi: text('kpi'), state: text('state'), video: text('video'), playback: text('playback'),
  captions: text('captions'), transcript: text('transcript'), attention: text('attention') === '1',
})
const page = ref(Number(text('page')) || 1)
const search = ref(filters.value.q ?? '')

const health = ref<PublisherHealth | null>(null)
const healthError = ref('')
const alertData = ref<PublisherAlerts | null>(null)
const alertsError = ref('')
const summary = ref<PublishingSummary | null>(null)
const table = ref<PublishingPage | null>(null)
const loading = ref(true)
const refreshing = ref(false)
const error = ref('')
const refreshedAt = ref<string>('')
const openLecture = ref<string | null>(text('lecture') || null)
let timer: ReturnType<typeof setInterval> | undefined

const activeCount = computed(() => Object.values(filters.value).filter(v => v === true || (typeof v === 'string' && v)).length)
const hiddenActive = computed(() => ['captions', 'transcript'].filter(k => filters.value[k as keyof PublishingFilters]).length)
const pages = computed(() => Math.max(1, Math.ceil((table.value?.total ?? 0) / PAGE_SIZE)))
const activeKpi = computed(() => summary.value?.kpis.find(k => k.key === filters.value.kpi))

/** The publisher's alerts with the signed-in user's own read / dismissed state. */
async function loadAlerts() {
  try {
    alertData.value = await getPublisherAlerts()
    alertsError.value = ''
  } catch (caught) {
    alertsError.value = errorMessage(caught, 'The publisher alerts could not be read.')
  }
}

async function loadHealth() {
  try {
    health.value = await getPublisherHealth()
    healthError.value = ''
  } catch (caught) {
    health.value = null
    healthError.value = errorMessage(caught, 'Publisher health could not be read.')
  }
}

async function loadData(quiet: boolean) {
  if (rangeProblem.value) return
  if (!quiet) loading.value = true
  try {
    const { from, to } = range.value
    const [nextSummary, nextTable] = await Promise.all([
      getPublishingSummary(from, to),
      getPublishingLectures(from, to, filters.value, page.value, PAGE_SIZE),
    ])
    summary.value = nextSummary
    table.value = nextTable
    error.value = ''
  } catch (caught) {
    error.value = errorMessage(caught, 'The publishing monitor could not be loaded.')
  } finally {
    loading.value = false
  }
}

async function refresh(quiet = true) {
  refreshing.value = true
  await Promise.all([loadHealth(), loadAlerts(), loadData(quiet)])
  refreshedAt.value = new Date().toISOString()
  refreshing.value = false
}

function syncQuery() {
  const f = filters.value
  router.replace({ query: {
    range: preset.value === 'today' ? undefined : preset.value,
    from: preset.value === 'custom' ? customFrom.value : undefined,
    to: preset.value === 'custom' ? customTo.value : undefined,
    q: f.q || undefined, kpi: f.kpi || undefined, state: f.state || undefined, video: f.video || undefined,
    playback: f.playback || undefined, captions: f.captions || undefined, transcript: f.transcript || undefined,
    attention: f.attention ? '1' : undefined, page: page.value > 1 ? String(page.value) : undefined,
    lecture: openLecture.value ?? undefined,
  } })
}

function resetFilters() {
  filters.value = { q: '', kpi: '', state: '', video: '', playback: '', captions: '', transcript: '', attention: false }
  search.value = ''
}

function chooseKpi(key: string) {
  filters.value = { ...filters.value, kpi: filters.value.kpi === key || key === 'discovered' ? '' : key }
}

let searchDelay: ReturnType<typeof setTimeout> | undefined
watch(search, (value) => {
  if (searchDelay) clearTimeout(searchDelay)
  searchDelay = setTimeout(() => { filters.value = { ...filters.value, q: value.trim() } }, 350)
})
watch([preset, customFrom, customTo], () => { page.value = 1; syncQuery(); loadData(false) })
watch(filters, () => { page.value = 1; syncQuery(); loadData(true) }, { deep: true })
watch(page, () => { syncQuery(); loadData(true) })
watch(openLecture, syncQuery)

function onVisibility() {
  if (document.visibilityState === 'visible') refresh()
}

onMounted(async () => {
  await refresh(false)
  timer = setInterval(() => { if (document.visibilityState === 'visible') refresh() }, REFRESH_MS)
  document.addEventListener('visibilitychange', onVisibility)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  if (searchDelay) clearTimeout(searchDelay)
  document.removeEventListener('visibilitychange', onVisibility)
})

const discovery = computed(() => health.value?.tasks.DISCOVERY ?? null)
const autoRelease = computed(() => health.value?.tasks.AUTO_RELEASE ?? null)
/** Every stored severity, counted separately: "3 Warnings · 1 Digest" (the digest is informational). */
/** NEW (unread) actionable alerts for the signed-in user, per stored severity; the digest is never unread. */
const newAlerts = computed(() => newAlertCounts(alertData.value?.review_counts.new_by_severity))
/** Open alerts the user has read but not dismissed: still open, no longer new. */
const readOpen = computed(() => alertData.value?.review_counts.read ?? 0)
const ALERT_TONE: Record<string, string> = { CRITICAL: 'text-rose-700', WARNING: 'text-orange-700' }
const showAlerts = ref(false)
/** The figure that answers the page's question: what the LMS can play right now (StatTile's `lead`). */
const LEAD_KPI = 'playable'
const releasedCount = computed(() => summary.value?.kpis.find(k => k.key === 'released')?.count ?? 0)
/** Colour only when the number is not zero (zeros are muted above). Full class names for Tailwind. */
const KPI_TONE: Record<string, string> = {
  missing: 'text-amber-700', attention: 'text-orange-700',
}
</script>

<template>
  <div class="page stack">
    <PageHeader
      kicker="Publishing"
      title="Lecture Publishing Monitor"
      lede="Teams recording → LMS match → Bunny import → encoding verification → automatic release → LMS playback. Read-only; captions are optional for playback."
    >
      <template #actions>
        <span class="text-xs text-muted" aria-live="polite">
          <template v-if="refreshedAt">Last updated {{ cairoDateTime(refreshedAt) }} · auto-refresh every minute</template>
        </span>
        <button type="button" class="btn-secondary" :disabled="refreshing" @click="refresh(true)">
          <AppIcon name="refresh" :size="16" :class="refreshing ? 'animate-spin' : ''" /> Refresh
        </button>
      </template>
    </PageHeader>

    <!-- A. Publisher status -->
    <section class="surface px-4 py-3" aria-label="Publisher status">
      <div v-if="healthError" class="flex flex-wrap items-center gap-2 text-sm">
        <StatusBadge label="Status unavailable" tone="neutral" />
        <span class="text-muted">{{ healthError }} The publisher's health is not known right now.</span>
      </div>
      <div v-else-if="!health" class="skeleton h-10 w-full" />
      <dl v-else class="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-4 xl:grid-cols-8">
        <div>
          <dt class="tile-label">Publisher</dt>
          <dd class="mt-1"><StatusBadge v-bind="label.health(health.status)" /></dd>
        </div>
        <div>
          <dt class="tile-label">Scheduler heartbeat</dt>
          <dd class="mt-1">
            <template v-if="health.scheduler">
              <span :class="health.scheduler.state === 'OK' ? 'text-ink' : 'font-semibold text-rose-700'">{{ age(health.scheduler.age_seconds) }} ago</span>
              <span v-if="health.scheduler.state !== 'OK'" class="ml-1 text-2xs text-rose-700">{{ label.health(health.scheduler.state).label }}</span>
            </template>
            <span v-else class="text-muted">No heartbeat recorded</span>
          </dd>
        </div>
        <div>
          <dt class="tile-label">Workers</dt>
          <dd class="mt-1" :title="health.workers.map(w => `${w.name}: ${w.state} (${age(w.age_seconds)} ago)`).join('\n')">
            <span :class="health.workers_seen && health.workers_healthy === health.workers_seen ? 'text-ink' : 'font-semibold text-orange-700'">
              {{ health.workers_healthy }} / {{ health.workers_seen }} healthy
            </span>
          </dd>
        </div>
        <div>
          <dt class="tile-label">Last discovery</dt>
          <dd class="mt-1">
            {{ discovery?.last_succeeded_at ? sinceNow(discovery.last_succeeded_at) : 'Never' }}
            <span v-if="discovery?.last_status === 'FAILED'" class="text-2xs text-rose-700">last run failed</span>
          </dd>
        </div>
        <div>
          <dt class="tile-label">Auto-release</dt>
          <dd class="mt-1">
            <StatusBadge v-bind="label.autoRelease(health.auto_release)" />
            <p v-if="autoRelease?.last_succeeded_at" class="mt-0.5 text-2xs text-muted">
              succeeded {{ sinceNow(autoRelease.last_succeeded_at) }}<template v-if="autoRelease.detail.released !== undefined"> · released {{ autoRelease.detail.released }}</template>
            </p>
            <p v-if="autoRelease?.last_status === 'FAILED'" class="mt-0.5 text-2xs text-rose-700">last run failed: {{ autoRelease.detail.code ?? autoRelease.detail.error }}</p>
          </dd>
        </div>
        <div>
          <dt class="tile-label">Bunny library</dt>
          <dd class="mt-1 tabular-nums">{{ health.library_id ?? '—' }}</dd>
        </div>
        <div>
          <dt class="tile-label">LMS playback</dt>
          <dd class="mt-1"><StatusBadge :label="health.playback_enabled ? 'Switched on' : 'Switched off'" :tone="health.playback_enabled ? 'ok' : 'neutral'" /></dd>
        </div>
        <div>
          <dt class="tile-label">Alerts</dt>
          <dd class="mt-1">
            <button
              v-if="alertData" type="button" class="row-link text-left font-semibold"
              :aria-expanded="showAlerts" aria-controls="publisher-alerts" data-test="alert-counts"
              @click="showAlerts = !showAlerts"
            >
              <template v-for="(part, index) in newAlerts" :key="part.severity">
                <span v-if="index" class="text-faint" aria-hidden="true"> · </span>
                <span :class="ALERT_TONE[part.severity] ?? 'text-ink'">{{ part.text }}</span>
              </template>
              <span v-if="!newAlerts.length" class="font-normal text-muted">No new alerts</span>
              <span v-if="readOpen" class="font-normal text-muted"> · {{ readOpen }} read, still open</span>
              <span class="ml-2 text-2xs font-semibold text-brand-700 underline">{{ showAlerts ? 'Hide' : 'View' }}</span>
            </button>
            <span v-else-if="alertsError" class="text-xs text-muted">{{ alertsError }}</span>
            <span v-else class="text-muted">—</span>
          </dd>
        </div>
      </dl>
    </section>

    <PublisherAlertsPanel
      v-if="showAlerts" id="publisher-alerts" :data="alertData" :error="alertsError"
      @close="showAlerts = false" @open-lecture="(id) => { openLecture = id }" @changed="loadAlerts"
    />

    <!-- Range -->
    <section class="flex flex-wrap items-center gap-2" aria-label="Date range">
      <div class="inline-flex rounded-lg border border-line bg-white p-0.5" role="group">
        <button
          v-for="option in ([['today', 'Today'], ['yesterday', 'Yesterday'], ['last7', 'Last 7 days'], ['custom', 'Custom']] as const)"
          :key="option[0]" type="button"
          class="rounded-md px-3 py-1.5 text-[0.8125rem] font-semibold transition"
          :class="preset === option[0] ? 'bg-brand-700 text-white' : 'text-muted hover:text-brand-700'"
          :aria-pressed="preset === option[0]" @click="preset = option[0]"
        >
          {{ option[1] }}
        </button>
      </div>
      <template v-if="preset === 'custom'">
        <label class="filter-field"><span class="filter-label">From</span><input v-model="customFrom" type="date" class="field field-sm"></label>
        <label class="filter-field"><span class="filter-label">To</span><input v-model="customTo" type="date" class="field field-sm"></label>
      </template>
      <span class="text-sm text-muted">
        {{ businessDate(range.from) }}<template v-if="range.to !== range.from"> – {{ businessDate(range.to) }}</template> · Cairo time
      </span>
      <p v-if="rangeProblem" class="notice-error w-full">{{ rangeProblem }}</p>
    </section>

    <LoadingSkeleton v-if="loading" variant="cards" :rows="8" />
    <ErrorState v-else-if="error && !summary" :message="error" @retry="refresh(false)" />

    <template v-else-if="summary">
      <p v-if="error" class="notice-warn" role="status"><AppIcon name="alert" :size="16" class="mt-0.5" />{{ error }} Showing the last data received.</p>

      <!-- B. KPIs: each one is also the filter for its rows -->
      <section class="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-9" aria-label="Publishing KPIs">
        <button
          v-for="kpi in summary.kpis" :key="kpi.key" type="button"
          class="text-left transition hover:ring-2 hover:ring-brand-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
          :class="[kpi.key === LEAD_KPI ? 'tile-lead sm:col-span-2 xl:col-span-2' : 'tile',
                   (filters.kpi || 'discovered') === kpi.key ? (kpi.key === LEAD_KPI ? 'ring-2 ring-gold-400' : 'ring-2 ring-brand-600') : '']"
          :title="kpi.definition" :aria-pressed="(filters.kpi || 'discovered') === kpi.key"
          @click="chooseKpi(kpi.key)"
        >
          <template v-if="kpi.key === LEAD_KPI">
            <p class="tile-label !text-white/75">{{ kpi.label }}</p>
            <p class="tile-value !text-4xl !text-white">{{ kpi.count }}</p>
            <p class="tile-note !text-white/75">
              of {{ summary.total }} lecture{{ summary.total === 1 ? '' : 's' }} in range · {{ releasedCount }} released
            </p>
          </template>
          <template v-else>
            <p class="tile-label">{{ kpi.label }}</p>
            <p class="tile-value" :class="kpi.count === 0 && kpi.key !== 'discovered' ? '!text-faint' : (KPI_TONE[kpi.key] ?? 'text-ink')">
              {{ kpi.count }}
            </p>
          </template>
        </button>
      </section>
      <p v-if="summary.playable_without_captions" class="-mt-3 text-xs text-muted">
        {{ summary.playable_without_captions }} playable lecture{{ summary.playable_without_captions === 1 ? '' : 's' }} without captions yet: captions are optional and are added to the same video when ready.
      </p>
      <p v-if="summary.states.NOT_TRACKED" class="-mt-3 text-xs text-muted">
        {{ summary.states.NOT_TRACKED }} lecture{{ summary.states.NOT_TRACKED === 1 ? ' has' : 's have' }} not been picked up by the publisher yet
        (before its start date, or awaiting the historical backfill). They are not counted as missing recordings.
        <button type="button" class="btn-quiet !text-xs" @click="filters = { ...filters, kpi: '', state: 'NOT_TRACKED' }">Show them</button>
      </p>

      <!-- D. Filters -->
      <FilterBar :active-count="activeCount" :hidden-active="hiddenActive" @reset="resetFilters">
        <label class="filter-field min-w-[14rem] flex-1">
          <AppIcon name="search" :size="15" class="text-faint" />
          <input v-model="search" type="search" class="field field-sm" placeholder="Search title, module, LMS occurrence" aria-label="Search lectures">
        </label>
        <label class="filter-field">
          <span class="filter-label">Bunny</span>
          <select v-model="filters.video" class="field field-sm w-auto">
            <option value="">Any</option>
            <option value="NOT_IMPORTED">Not imported</option>
            <option value="UPLOADING">Uploading</option>
            <option value="IN_PROGRESS">Encoding</option>
            <option value="VERIFIED">Verified</option>
            <option value="FAILED">Failed / review</option>
          </select>
        </label>
        <label class="filter-field">
          <span class="filter-label">LMS</span>
          <select v-model="filters.playback" class="field field-sm w-auto">
            <option value="">Any</option>
            <option value="AVAILABLE">Available</option>
            <option value="NOT_AVAILABLE">Not available</option>
          </select>
        </label>
        <label class="filter-field">
          <span class="filter-label">State</span>
          <select v-model="filters.state" class="field field-sm w-auto">
            <option value="">Any</option>
            <option v-for="(badge, key) in MONITOR_STATE" :key="key" :value="key">{{ badge.label }}</option>
          </select>
        </label>
        <label class="flex items-center gap-1.5 text-[0.8125rem] font-semibold text-ink">
          <input v-model="filters.attention" type="checkbox" class="h-4 w-4 rounded border-line"> Needs attention only
        </label>
        <template #more>
          <label class="filter-field">
            <span class="filter-label">Transcript</span>
            <select v-model="filters.transcript" class="field field-sm w-auto">
              <option value="">Any</option>
              <option v-for="(badge, key) in TRANSCRIPT" :key="key" :value="key">{{ badge.label }}</option>
            </select>
          </label>
          <label class="filter-field">
            <span class="filter-label">Captions</span>
            <select v-model="filters.captions" class="field field-sm w-auto">
              <option value="">Any</option>
              <option v-for="(badge, key) in CAPTIONS" :key="key" :value="key">{{ badge.label }}</option>
            </select>
          </label>
        </template>
      </FilterBar>

      <!-- C. Daily lecture tracking -->
      <SectionPanel
        :title="activeKpi && activeKpi.key !== 'discovered' ? activeKpi.label : 'Lectures'"
        :count="table?.total ?? 0"
        :note="activeKpi && activeKpi.key !== 'discovered' ? activeKpi.definition : 'One row per lecture. Select a row for the full publishing history.'"
        flush
      >
        <EmptyState
          v-if="!table?.results.length" compact tone="neutral" icon="inbox"
          :title="summary.total ? 'No lecture matches these filters' : 'No lectures in this range'"
          :message="summary.total ? 'Change or reset the filters.' : 'No lecture was discovered for these dates.'"
        />
        <div v-else class="overflow-x-auto">
          <table class="data-table">
            <thead>
              <tr>
                <th scope="col">Lecture</th>
                <th scope="col">State</th>
                <th scope="col">LMS match</th>
                <th scope="col">Teams recording</th>
                <th scope="col">Bunny import</th>
                <th scope="col">Video</th>
                <th scope="col">Transcript</th>
                <th scope="col">Captions</th>
                <th scope="col">Release</th>
                <th scope="col">LMS playback</th>
                <th scope="col">Updated</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in table.results" :key="row.lecture_id"
                class="cursor-pointer" :class="row.needs_attention ? 'bg-orange-50/40' : ''"
                tabindex="0" @click="openLecture = row.lecture_id" @keydown.enter="openLecture = row.lecture_id"
              >
                <td class="min-w-[15rem]">
                  <p class="font-semibold text-ink">{{ row.subject ?? 'Lecture' }}</p>
                  <p class="text-2xs text-muted">
                    {{ businessDate(row.session_date) }} · {{ cairoTimeRange(row.scheduled_start, row.scheduled_end) }}<template v-if="row.module"> · {{ row.module }}</template>
                  </p>
                </td>
                <td class="min-w-[14rem]">
                  <StatusBadge v-bind="label.state(row.state)" />
                  <p class="mt-0.5 max-w-xs text-2xs leading-snug text-muted">{{ row.reason }}</p>
                  <p v-if="row.retry_at" class="text-2xs text-faint">next check {{ cairoDateTime(row.retry_at) }}</p>
                </td>
                <td><StatusBadge v-bind="label.lms(row.lms.status)" :title="row.lms.occurrence_id" /></td>
                <td>
                  <StatusBadge v-bind="label.recording(row.recording.status)" :title="row.recording.reason" />
                  <p v-if="row.recording.expected_parts && row.recording.expected_parts > 1" class="text-2xs text-muted">{{ row.recording.located_parts }}/{{ row.recording.expected_parts }} parts</p>
                </td>
                <td><StatusBadge v-bind="label.bunny(row.bunny.status)" /></td>
                <td><StatusBadge v-bind="label.video(row.video.status)" /></td>
                <td><StatusBadge v-bind="label.transcript(row.transcript.status)" /></td>
                <td><StatusBadge v-bind="label.captions(row.captions.status)" /></td>
                <td><StatusBadge v-bind="label.release(row.release.status)" /></td>
                <td>
                  <StatusBadge :label="row.playback.available ? 'Available' : 'Not available'" :tone="row.playback.available ? 'ok' : 'quiet'" :title="row.playback.reason" />
                </td>
                <td class="whitespace-nowrap text-xs text-muted">{{ sinceNow(row.updated_at, '—') }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="pages > 1" class="flex items-center justify-between border-t border-line px-4 py-2.5 text-sm">
          <span class="text-muted">Page {{ page }} of {{ pages }}</span>
          <div class="flex gap-2">
            <button type="button" class="btn-secondary btn-sm" :disabled="page <= 1" @click="page -= 1"><AppIcon name="chevronLeft" :size="14" /> Previous</button>
            <button type="button" class="btn-secondary btn-sm" :disabled="page >= pages" @click="page += 1">Next <AppIcon name="chevronRight" :size="14" /></button>
          </div>
        </div>
      </SectionPanel>

      <p class="text-2xs text-faint">
        Counts are lectures (never video parts or job attempts) dated in the range in Cairo time. Data generated {{ cairoDateTime(summary.generated_at) }} from the publisher database; nothing on this page calls Teams or Bunny.
      </p>
    </template>

    <PublishingLectureDrawer v-if="openLecture" :lecture-id="openLecture" @close="openLecture = null" />
  </div>
</template>
