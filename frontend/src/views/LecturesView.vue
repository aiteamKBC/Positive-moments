<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import ErrorState from '../components/ErrorState.vue'
import LoadingSkeleton from '../components/LoadingSkeleton.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { getLectures, getSummary } from '../services/api'
import type { Lecture, PaginatedLectures, Summary } from '../types'
import { errorMessage, formatDate, openSafely } from '../utils/format'

const router = useRouter()
const summary = ref<Summary | null>(null)
const data = ref<PaginatedLectures | null>(null)
const loading = ref(true)
const error = ref('')
const searchInput = ref('')
const filters = reactive({
  search: '',
  trainer: '',
  date_from: '',
  date_to: '',
  has_positive_clips: '',
  recording_status: '',
  clips_status: '',
  clip_production_status: '',
  page: 1,
  page_size: 20,
})
let searchTimer: ReturnType<typeof setTimeout> | undefined
let requestCounter = 0

const trainers = computed(() => {
  const values = (data.value?.results ?? [])
    .map((lecture) => lecture.trainer)
    .filter((value): value is string => Boolean(value))
  return [...new Set(values)].sort()
})

const cards = computed(() => [
  {
    label: 'Processed lectures',
    value: summary.value?.processed_lectures ?? 0,
    tone: 'bg-slate-100 text-slate-700',
    icon: 'M8 6h8M8 10h8m-8 4h5m-7 7h12a3 3 0 0 0 3-3V6a3 3 0 0 0-3-3H6a3 3 0 0 0-3 3v12a3 3 0 0 0 3 3Z',
  },
  {
    label: 'With positive mentions',
    value: summary.value?.lectures_with_positive_clips ?? 0,
    tone: 'bg-emerald-50 text-emerald-700',
    icon: 'm8 12 3 3 5-6m5 3a9 9 0 1 1-5-8.1',
  },
  {
    label: 'Positive moments',
    value: summary.value?.total_positive_clips ?? 0,
    tone: 'bg-cyan-50 text-cyan-700',
    icon: 'M12 21s-7-4.35-7-11a4 4 0 0 1 7-2.65A4 4 0 0 1 19 10c0 6.65-7 11-7 11Z',
  },
  {
    label: 'Missing recording links',
    value: summary.value?.recordings_missing ?? 0,
    tone: 'bg-amber-50 text-amber-700',
    icon: 'M12 9v4m0 4h.01M10.3 3.7 2.7 17a2 2 0 0 0 1.74 3h15.12a2 2 0 0 0 1.74-3L13.7 3.7a2 2 0 0 0-3.4 0Z',
  },
])

function queryParams() {
  return Object.fromEntries(
    Object.entries(filters).filter(([, value]) => value !== ''),
  ) as Record<string, string | number>
}

function summaryParams() {
  return Object.fromEntries(
    Object.entries(queryParams()).filter(([key]) => !['page', 'page_size'].includes(key)),
  )
}

async function load() {
  const currentRequest = ++requestCounter
  loading.value = true
  error.value = ''
  try {
    const [summaryResult, lectureResult] = await Promise.all([
      getSummary(summaryParams()),
      getLectures(queryParams()),
    ])
    if (currentRequest !== requestCounter) return
    summary.value = summaryResult
    data.value = lectureResult
  } catch (caught) {
    if (currentRequest !== requestCounter) return
    error.value = errorMessage(caught, 'The lecture dashboard could not be loaded.')
  } finally {
    if (currentRequest === requestCounter) loading.value = false
  }
}

function resetFilters() {
  Object.assign(filters, {
    search: '',
    trainer: '',
    date_from: '',
    date_to: '',
    has_positive_clips: '',
    recording_status: '',
    clips_status: '',
    clip_production_status: '',
    page: 1,
    page_size: 20,
  })
  searchInput.value = ''
}

function changePage(page: number) {
  filters.page = page
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function watchFull(lecture: Lecture) {
  if (lecture.recording_url) openSafely(lecture.recording_url)
}

watch(searchInput, (value) => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    filters.search = value.trim()
    filters.page = 1
  }, 350)
})

watch(
  () => [
    filters.search,
    filters.trainer,
    filters.date_from,
    filters.date_to,
    filters.has_positive_clips,
    filters.recording_status,
    filters.clips_status,
    filters.clip_production_status,
    filters.page_size,
  ],
  () => {
    if (filters.page !== 1) filters.page = 1
    else void load()
  },
)

watch(() => filters.page, load)

onMounted(load)
</script>

<template>
  <div class="w-full px-4 py-8 sm:px-6 lg:px-8 lg:py-10">
    <div class="flex flex-col justify-between gap-3 sm:flex-row sm:items-end">
      <div>
        <p class="text-sm font-semibold text-brand-700">Lecture intelligence</p>
        <h1 class="mt-1 text-3xl font-bold tracking-tight sm:text-4xl">Positive moments</h1>
        <p class="mt-2 max-w-2xl text-sm text-muted sm:text-base">
          Find the moments where learners shared strong outcomes, appreciation, and intent to apply.
        </p>
      </div>
      <p class="text-xs font-medium text-slate-400">V5 analysis</p>
    </div>

    <div v-if="summary" class="mt-7 grid grid-cols-2 gap-3 lg:grid-cols-4">
      <article v-for="card in cards" :key="card.label" class="surface p-4 sm:p-5">
        <span class="grid h-9 w-9 place-items-center rounded-xl" :class="card.tone">
          <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2">
            <path :d="card.icon" />
          </svg>
        </span>
        <p class="mt-4 text-2xl font-bold tracking-tight sm:text-3xl">{{ card.value }}</p>
        <p class="mt-1 text-xs font-medium text-muted sm:text-sm">{{ card.label }}</p>
      </article>
    </div>
    <div v-else class="mt-7 grid grid-cols-2 gap-3 lg:grid-cols-4">
      <div v-for="item in 4" :key="item" class="surface h-36 animate-pulse bg-slate-100" />
    </div>

    <section class="surface mt-7 p-4 sm:p-5" aria-label="Lecture filters">
      <div class="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <div class="md:col-span-2">
          <label for="search" class="label">Search</label>
          <div class="relative">
            <svg viewBox="0 0 24 24" class="absolute left-3 top-3.5 h-4 w-4 text-slate-400" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>
            </svg>
            <input id="search" v-model="searchInput" class="field pl-10" placeholder="Search subject or trainer…" />
          </div>
        </div>
        <div>
          <label for="trainer" class="label">Trainer</label>
          <select id="trainer" v-model="filters.trainer" class="field">
            <option value="">All trainers</option>
            <option v-for="trainer in trainers" :key="trainer" :value="trainer">{{ trainer }}</option>
          </select>
        </div>
        <div>
          <label for="status" class="label">Analysis status</label>
          <select id="status" v-model="filters.clips_status" class="field">
            <option value="">All statuses</option>
            <option value="completed">Completed</option>
            <option value="processing">Processing</option>
            <option value="error">Error</option>
          </select>
        </div>
        <div>
          <label for="date-from" class="label">From date</label>
          <input id="date-from" v-model="filters.date_from" type="date" class="field" />
        </div>
        <div>
          <label for="date-to" class="label">To date</label>
          <input id="date-to" v-model="filters.date_to" type="date" class="field" />
        </div>
        <div>
          <label for="mentions" class="label">Positive mentions</label>
          <select id="mentions" v-model="filters.has_positive_clips" class="field">
            <option value="">With or without</option>
            <option value="true">With positive mentions</option>
            <option value="false">Without positive mentions</option>
          </select>
        </div>
        <div>
          <label for="clip-video" class="label">CLIP VIDEO</label>
          <select id="clip-video" v-model="filters.clip_production_status" class="field">
            <option value="">All clip statuses</option>
            <option value="ready">Ready</option>
            <option value="pending">Pending generation</option>
            <option value="no_positive_moments">No positive moments</option>
          </select>
        </div>
        <div>
          <label for="recording" class="label">Recording</label>
          <div class="flex gap-2">
            <select id="recording" v-model="filters.recording_status" class="field">
              <option value="">Any availability</option>
              <option value="available">Available</option>
              <option value="missing">Link pending</option>
            </select>
            <button type="button" class="btn-secondary shrink-0 !px-3" title="Reset filters" @click="resetFilters">
              Reset
            </button>
          </div>
        </div>
      </div>
    </section>

    <div class="mt-6 flex items-center justify-between">
      <h2 class="text-lg font-bold">Lectures</h2>
      <p v-if="data" class="text-sm text-muted">{{ data.count }} result{{ data.count === 1 ? '' : 's' }}</p>
    </div>

    <LoadingSkeleton v-if="loading && !data" class="mt-4" />
    <ErrorState v-else-if="error" class="mt-4" :message="error" @retry="load" />

    <template v-else-if="data">
      <div v-if="data.results.length" class="surface mt-4 hidden overflow-hidden lg:block">
        <table class="w-full text-left text-sm">
          <thead class="border-b bg-slate-50/80 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th class="px-5 py-4 font-semibold">Lecture</th>
              <th class="px-4 py-4 font-semibold">Date</th>
              <th class="px-4 py-4 font-semibold">Trainer</th>
              <th class="px-4 py-4 font-semibold">Status</th>
              <th class="px-4 py-4 text-center font-semibold">Moments</th>
              <th class="px-4 py-4 font-semibold">Clip video</th>
              <th class="px-4 py-4 font-semibold">Recording</th>
              <th class="px-5 py-4 text-right font-semibold">Actions</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            <tr v-for="lecture in data.results" :key="lecture.session_id" class="transition hover:bg-slate-50/60">
              <td class="max-w-xs px-5 py-4">
                <p class="truncate font-semibold text-ink">{{ lecture.subject || 'Untitled lecture' }}</p>
              </td>
              <td class="whitespace-nowrap px-4 py-4 text-muted">{{ formatDate(lecture.date) }}</td>
              <td class="max-w-44 truncate px-4 py-4 text-muted">{{ lecture.trainer || 'Not assigned' }}</td>
              <td class="px-4 py-4"><StatusBadge :value="lecture.clips_status" /></td>
              <td class="px-4 py-4 text-center">
                <span class="inline-grid h-8 min-w-8 place-items-center rounded-lg bg-brand-50 px-2 font-bold text-brand-700">
                  {{ lecture.positive_clips_count || 0 }}
                </span>
              </td>
              <td class="whitespace-nowrap px-4 py-4">
                <span v-if="lecture.ready_clips_count > 0" class="font-medium text-emerald-700">
                  {{ lecture.ready_clips_count }} clip{{ lecture.ready_clips_count === 1 ? '' : 's' }} ready
                </span>
                <span v-else-if="(lecture.positive_clips_count || 0) > 0" class="text-amber-700">Pending</span>
                <span v-else class="text-muted">No moments</span>
              </td>
              <td class="px-4 py-4">
                <span v-if="lecture.recording_available" class="font-medium text-emerald-700">Available</span>
                <span v-else class="text-amber-700">Link pending</span>
              </td>
              <td class="px-5 py-4">
                <div class="flex justify-end gap-2">
                  <button
                    type="button"
                    class="btn-secondary !px-3 !py-2"
                    :disabled="!lecture.recording_available"
                    :title="lecture.recording_available ? 'Open full lecture' : 'Recording link pending'"
                    @click="watchFull(lecture)"
                  >
                    {{ lecture.recording_available ? 'Watch full' : 'Link pending' }}
                  </button>
                  <button type="button" class="btn-primary !px-3 !py-2" @click="router.push(`/lectures/${lecture.session_key}`)">
                    View moments
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="data.results.length" class="mt-4 grid gap-4 lg:hidden">
        <article v-for="lecture in data.results" :key="lecture.session_id" class="surface p-5">
          <div class="flex items-start justify-between gap-3">
            <div>
              <p class="font-bold">{{ lecture.subject || 'Untitled lecture' }}</p>
              <p class="mt-1 text-sm text-muted">{{ lecture.trainer || 'Trainer not assigned' }}</p>
            </div>
            <StatusBadge :value="lecture.clips_status" />
          </div>
          <div class="mt-5 grid grid-cols-3 gap-2 border-y py-4 text-center">
            <div><p class="text-xs text-muted">Date</p><p class="mt-1 text-xs font-semibold">{{ formatDate(lecture.date) }}</p></div>
            <div><p class="text-xs text-muted">Moments</p><p class="mt-1 text-sm font-bold text-brand-700">{{ lecture.positive_clips_count || 0 }}</p></div>
            <div><p class="text-xs text-muted">Recording</p><p class="mt-1 text-xs font-semibold" :class="lecture.recording_available ? 'text-emerald-700' : 'text-amber-700'">{{ lecture.recording_available ? 'Available' : 'Pending' }}</p></div>
          </div>
          <p class="mt-3 text-sm font-medium" :class="lecture.ready_clips_count > 0 ? 'text-emerald-700' : 'text-amber-700'">
            <template v-if="lecture.ready_clips_count > 0">
              {{ lecture.ready_clips_count }} clip{{ lecture.ready_clips_count === 1 ? '' : 's' }} ready
            </template>
            <template v-else-if="(lecture.positive_clips_count || 0) > 0">Clip video pending</template>
            <template v-else>No positive moments</template>
          </p>
          <div class="mt-4 grid grid-cols-2 gap-2">
            <button type="button" class="btn-secondary" :disabled="!lecture.recording_available" @click="watchFull(lecture)">
              {{ lecture.recording_available ? 'Watch full' : 'Link pending' }}
            </button>
            <button type="button" class="btn-primary" @click="router.push(`/lectures/${lecture.session_key}`)">View moments</button>
          </div>
        </article>
      </div>

      <div v-else class="surface mt-4 flex flex-col items-center px-6 py-14 text-center">
        <span class="grid h-12 w-12 place-items-center rounded-full bg-slate-100 text-slate-500">
          <svg viewBox="0 0 24 24" class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>
          </svg>
        </span>
        <h3 class="mt-4 font-bold">No lectures found</h3>
        <p class="mt-1 text-sm text-muted">Try changing or resetting your filters.</p>
        <button type="button" class="btn-secondary mt-5" @click="resetFilters">Reset filters</button>
      </div>

      <nav v-if="data.total_pages > 1" class="mt-6 flex items-center justify-between" aria-label="Lecture pages">
        <button type="button" class="btn-secondary" :disabled="!data.previous" @click="changePage(data.page - 1)">Previous</button>
        <span class="text-sm font-medium text-muted">Page {{ data.page }} of {{ data.total_pages }}</span>
        <button type="button" class="btn-secondary" :disabled="!data.next" @click="changePage(data.page + 1)">Next</button>
      </nav>
    </template>
  </div>
</template>
