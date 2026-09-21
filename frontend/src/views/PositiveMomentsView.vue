<script setup lang="ts">
/**
 * Positive Moments: a FEATURE of the platform, not the platform.
 *
 * This page used to be the product. It is now one of three areas, and it has
 * its own question: what positive learner moments were found, and what media
 * exists for them? That is deliberately not the Lectures workspace's question,
 * so nothing here repeats a pipeline column, a stage chip or a next action.
 *
 * HONESTY ABOUT MEDIA
 * -------------------
 * Detecting a moment and producing a clip are different things, and the second
 * one lags the first. "Clips ready" counts real uploaded assets; a lecture with
 * six moments and no cut media says "No clips yet" rather than borrowing the
 * moment count to look finished. No future media capability is mocked here.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'

import AppIcon from '../components/AppIcon.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import FilterBar from '../components/FilterBar.vue'
import LoadingSkeleton from '../components/LoadingSkeleton.vue'
import PageHeader from '../components/PageHeader.vue'
import SectionPanel from '../components/SectionPanel.vue'
import StatTile from '../components/StatTile.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { getLectures, getSummary } from '../services/api'
import type { Lecture, PaginatedLectures, Summary } from '../types'
import { businessDate } from '../utils/datetime'
import { humanise } from '../utils/labels'
import { errorMessage, openSafely } from '../utils/format'

const summary = ref<Summary | null>(null)
const data = ref<PaginatedLectures | null>(null)
const loading = ref(true)
const error = ref('')
const searchInput = ref('')

const filters = reactive({
  search: '', trainer: '', date_from: '', date_to: '',
  has_positive_clips: '', clip_production_status: '', recording_status: '', clips_status: '',
  page: 1, page_size: 20,
})

let searchTimer: ReturnType<typeof setTimeout> | undefined
let requestId = 0

const trainers = computed(() => [...new Set(
  (data.value?.results ?? []).map((row) => row.trainer).filter((name): name is string => Boolean(name)),
)].sort())

function queryParams() {
  return Object.fromEntries(
    Object.entries(filters).filter(([, value]) => value !== ''),
  ) as Record<string, string | number>
}

async function load() {
  const current = ++requestId
  loading.value = true
  error.value = ''
  try {
    const params = queryParams()
    const withoutPaging = Object.fromEntries(
      Object.entries(params).filter(([key]) => !['page', 'page_size'].includes(key)),
    )
    const [totals, page] = await Promise.all([getSummary(withoutPaging), getLectures(params)])
    if (current !== requestId) return
    summary.value = totals
    data.value = page
  } catch (caught) {
    if (current !== requestId) return
    error.value = errorMessage(caught, 'Positive Moments could not be loaded.')
  } finally {
    if (current === requestId) loading.value = false
  }
}

watch(searchInput, (value) => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => { filters.search = value.trim(); filters.page = 1 }, 350)
})

watch(() => [filters.search, filters.trainer, filters.date_from, filters.date_to,
  filters.has_positive_clips, filters.clip_production_status, filters.recording_status,
  filters.clips_status], () => {
  if (filters.page !== 1) filters.page = 1
  else void load()
})
watch(() => filters.page, load)
onMounted(load)

function reset() {
  Object.assign(filters, {
    search: '', trainer: '', date_from: '', date_to: '',
    has_positive_clips: '', clip_production_status: '', recording_status: '', clips_status: '',
    page: 1, page_size: 20,
  })
  searchInput.value = ''
}

const activeCount = computed(() => [filters.search, filters.trainer, filters.date_from, filters.date_to,
  filters.has_positive_clips, filters.clip_production_status, filters.recording_status,
  filters.clips_status].filter(Boolean).length)
const hiddenActive = computed(() => [filters.date_from, filters.date_to,
  filters.clip_production_status, filters.recording_status, filters.clips_status].filter(Boolean).length)

/** Clip production, stated as it is - never inferred from the moment count. */
function clipState(row: Lecture) {
  if (row.ready_clips_count > 0) {
    return { label: `${row.ready_clips_count} clip${row.ready_clips_count === 1 ? '' : 's'} ready`, tone: 'ok' as const }
  }
  if (row.moment_count > 0) return { label: 'No clips yet', tone: 'waiting' as const }
  return { label: 'Nothing to clip', tone: 'quiet' as const }
}

function changePage(page: number) {
  filters.page = page
  window.scrollTo({ top: 0, behavior: 'smooth' })
}
</script>

<template>
  <div class="page stack">
    <PageHeader
      kicker="Positive Moments"
      title="Learner moments"
      lede="Appreciation, breakthroughs and positive experiences found in analysed lectures — and the clip media produced from them."
    />

    <section class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <StatTile
        lead label="Lectures analysed" :value="summary?.processed_lectures" :loading="loading && !summary"
        note="Completed V5 positive-moment analysis."
      />
      <StatTile
        label="With positive moments" :value="summary?.lectures_with_positive_clips"
        tone="ok" icon="moments" :loading="loading && !summary"
        :note="summary ? `${summary.lectures_without_positive_clips} without` : undefined"
      />
      <StatTile
        label="Positive moments" :value="summary?.total_positive_clips" icon="quote"
        :loading="loading && !summary" note="Individual moments across those lectures."
      />
      <StatTile
        label="Clips ready" :value="summary?.ready_clips" icon="video" :loading="loading && !summary"
        :note="summary ? `Across ${summary.lectures_with_ready_clips} lectures. Clip production runs behind detection.` : undefined"
      />
    </section>

    <FilterBar :active-count="activeCount" :hidden-active="hiddenActive" @reset="reset">
      <div class="relative min-w-[14rem] flex-1">
        <AppIcon name="search" :size="16" class="pointer-events-none absolute left-3 top-2.5 text-faint" />
        <label class="sr-only" for="moment-search">Search</label>
        <input id="moment-search" v-model="searchInput" class="field pl-9" placeholder="Search lecture or trainer" />
      </div>
      <div class="filter-field">
        <span class="filter-label">Trainer</span>
        <label class="sr-only" for="moment-trainer">Trainer</label>
        <select id="moment-trainer" v-model="filters.trainer" class="field !w-auto">
          <option value="">All</option>
          <option v-for="name in trainers" :key="name" :value="name">{{ name }}</option>
        </select>
      </div>
      <div class="filter-field">
        <span class="filter-label">Moments</span>
        <label class="sr-only" for="has-moments">Has positive moments</label>
        <select id="has-moments" v-model="filters.has_positive_clips" class="field !w-auto">
          <option value="">With or without</option>
          <option value="true">With moments</option>
          <option value="false">Without moments</option>
        </select>
      </div>

      <template #more>
        <div class="filter-field">
          <span class="filter-label">Clips</span>
          <label class="sr-only" for="clip-status">Clip status</label>
          <select id="clip-status" v-model="filters.clip_production_status" class="field !w-auto">
            <option value="">Any</option>
            <option value="ready">Clips ready</option>
            <option value="pending">Awaiting clip production</option>
            <option value="no_positive_moments">Nothing to clip</option>
          </select>
        </div>
        <div class="filter-field">
          <span class="filter-label">Recording</span>
          <label class="sr-only" for="moment-recording">Recording</label>
          <select id="moment-recording" v-model="filters.recording_status" class="field !w-auto">
            <option value="">Any</option>
            <option value="available">Available</option>
            <option value="missing">Link pending</option>
          </select>
        </div>
        <div class="filter-field">
          <span class="filter-label">Analysis</span>
          <label class="sr-only" for="analysis">Analysis status</label>
          <select id="analysis" v-model="filters.clips_status" class="field !w-auto">
            <option value="">Any</option>
            <option value="completed">Completed</option>
            <option value="processing">Processing</option>
            <option value="error">Error</option>
          </select>
        </div>
        <div class="filter-field">
          <span class="filter-label">From</span>
          <label class="sr-only" for="from">From date</label>
          <input id="from" v-model="filters.date_from" type="date" class="field !w-auto" />
        </div>
        <div class="filter-field">
          <span class="filter-label">To</span>
          <label class="sr-only" for="to">To date</label>
          <input id="to" v-model="filters.date_to" type="date" class="field !w-auto" />
        </div>
      </template>
    </FilterBar>

    <LoadingSkeleton v-if="loading && !data" :rows="8" />
    <ErrorState v-else-if="error" :message="error" @retry="load" />

    <SectionPanel v-else-if="data" title="Analysed lectures" :count="data.count" flush>
      <EmptyState
        v-if="!data.results.length"
        tone="neutral"
        icon="moments"
        title="No analysed lectures match these filters"
        message="Try clearing a filter, or widen the date range."
      >
        <template #action><button type="button" class="btn-secondary" @click="reset">Reset filters</button></template>
      </EmptyState>

      <div v-else class="overflow-x-auto">
        <table class="data-table min-w-[1000px]">
          <thead>
            <tr>
              <th scope="col">Lecture</th>
              <th scope="col">Trainer</th>
              <th scope="col">Date</th>
              <th scope="col" class="text-center">Moments</th>
              <th scope="col">What learners said</th>
              <th scope="col">Clips</th>
              <th scope="col">Recording</th>
              <th scope="col"><span class="sr-only">Open</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in data.results" :key="row.session_id">
              <td class="max-w-[22rem]">
                <RouterLink
                  class="row-link"
                  :to="{ name: 'positive-moment-detail', params: { sessionKey: row.session_key } }"
                >
                  {{ row.subject || 'Untitled lecture' }}
                </RouterLink>
              </td>
              <td class="whitespace-nowrap text-body">{{ row.trainer || 'Not recorded' }}</td>
              <td class="whitespace-nowrap text-muted">{{ businessDate(row.date) }}</td>
              <td class="text-center">
                <span
                  class="inline-grid h-7 min-w-7 place-items-center rounded-lg px-2 text-sm font-bold tabular-nums"
                  :class="row.moment_count ? 'bg-brand-50 text-brand-700' : 'bg-slate-50 text-faint'"
                >{{ row.moment_count }}</span>
              </td>
              <td>
                <div v-if="row.top_categories.length" class="flex flex-wrap gap-1">
                  <span v-for="item in row.top_categories" :key="item.category" class="chip">
                    {{ humanise(item.category) }}<span class="ml-1 text-brand-500">{{ item.count }}</span>
                  </span>
                </div>
                <span v-else class="text-xs text-faint">No qualifying moments</span>
              </td>
              <td><StatusBadge :label="clipState(row).label" :tone="clipState(row).tone" /></td>
              <td>
                <button
                  v-if="row.recording_available"
                  type="button"
                  class="btn-quiet"
                  @click="row.recording_url && openSafely(row.recording_url)"
                >
                  <AppIcon name="play" :size="14" /> Watch
                </button>
                <StatusBadge v-else label="Link pending" tone="waiting" />
              </td>
              <td class="whitespace-nowrap text-right">
                <RouterLink
                  class="btn-quiet"
                  :to="{ name: 'positive-moment-detail', params: { sessionKey: row.session_key } }"
                  :aria-label="`View moments in ${row.subject}`"
                >
                  View moments <AppIcon name="arrowRight" :size="13" />
                </RouterLink>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <nav
        v-if="data.total_pages > 1"
        class="flex items-center justify-between border-t border-line px-4 py-3"
        aria-label="Pages"
      >
        <button type="button" class="btn-secondary btn-sm" :disabled="!data.previous" @click="changePage(data.page - 1)">
          <AppIcon name="chevronLeft" :size="14" /> Previous
        </button>
        <span class="text-xs text-muted">Page {{ data.page }} of {{ data.total_pages }}</span>
        <button type="button" class="btn-secondary btn-sm" :disabled="!data.next" @click="changePage(data.page + 1)">
          Next <AppIcon name="chevronRight" :size="14" />
        </button>
      </nav>
    </SectionPanel>
  </div>
</template>
