<script setup lang="ts">
/**
 * Social Media: lectures with AI-suggested content opportunities.
 *
 * A separate feature from Positive Moments and OFSTED Case Studies, read from
 * its own table. Every count is by primary format, so each opportunity is
 * counted once and the format columns add up to the total - the same numbers
 * the lecture page shows. Nothing here is approved for publishing.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppIcon from '../components/AppIcon.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import FilterBar from '../components/FilterBar.vue'
import LoadingSkeleton from '../components/LoadingSkeleton.vue'
import PageHeader from '../components/PageHeader.vue'
import SectionPanel from '../components/SectionPanel.vue'
import StatTile from '../components/StatTile.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { getSocialLectures, getSocialSummary } from '../services/social'
import type { PaginatedSocialLectures, SocialFormat, SocialSummary } from '../types/social'
import { businessDate } from '../utils/datetime'
import { errorMessage, openSafely } from '../utils/format'
import { NOT_APPROVED, SOCIAL_FORMAT_FILTERS } from '../utils/social'

const route = useRoute()
const router = useRouter()

const summary = ref<SocialSummary | null>(null)
const data = ref<PaginatedSocialLectures | null>(null)
const loading = ref(true)
const error = ref('')
const searchInput = ref('')

const FORMATS = SOCIAL_FORMAT_FILTERS.filter((option) => option.value) as { value: SocialFormat; label: string }[]
const fromQuery = (name: string) => (typeof route.query[name] === 'string' ? String(route.query[name]) : '')

const filters = reactive({
  // The format travels in the URL so a lecture page, and the way back, keep it.
  content_format: fromQuery('format'),
  search: '', date_from: '', date_to: '',
  page: 1, page_size: 20,
})

let searchTimer: ReturnType<typeof setTimeout> | undefined
let requestId = 0

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
    const [totals, page] = await Promise.all([getSocialSummary(withoutPaging), getSocialLectures(params)])
    if (current !== requestId) return
    summary.value = totals
    data.value = page
  } catch (caught) {
    if (current !== requestId) return
    error.value = errorMessage(caught, 'Social Media opportunities could not be loaded.')
  } finally {
    if (current === requestId) loading.value = false
  }
}

watch(searchInput, (value) => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => { filters.search = value.trim(); filters.page = 1 }, 350)
})

watch(() => [filters.content_format, filters.search, filters.date_from, filters.date_to], () => {
  if (filters.page !== 1) filters.page = 1
  else void load()
})
watch(() => filters.page, load)
watch(() => filters.content_format, (value) => {
  void router.replace({ query: { ...route.query, format: value || undefined } })
})
onMounted(load)

function reset() {
  Object.assign(filters, { content_format: '', search: '', date_from: '', date_to: '', page: 1, page_size: 20 })
  searchInput.value = ''
}

const activeCount = computed(() => [filters.content_format, filters.search, filters.date_from, filters.date_to]
  .filter(Boolean).length)
const hiddenActive = computed(() => [filters.date_from, filters.date_to].filter(Boolean).length)

const formatTotal = (value: string) => (value
  ? summary.value?.format_counts[value as SocialFormat]
  : summary.value?.opportunity_count)

function changePage(page: number) {
  filters.page = page
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

const detailLink = (sessionKey: string) => ({
  name: 'social-media-detail', params: { sessionKey },
  query: filters.content_format ? { format: filters.content_format } : {},
})
</script>

<template>
  <div class="page stack">
    <PageHeader
      kicker="Social Media"
      title="Content opportunities"
      lede="Reels, carousels, quote posts and educational posts the AI found in analysed lectures. Every one is a suggestion for review, not content approved for publishing."
    />

    <p class="notice-warn" role="note" data-test="not-approved-notice">
      <AppIcon name="info" :size="16" class="mt-0.5" />
      <span><strong>{{ NOT_APPROVED }}.</strong> These are AI-verified suggestions from lecture transcripts. Nobody has approved them for publishing.</span>
    </p>

    <section class="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
      <StatTile lead label="Lectures" :value="summary?.lectures" :loading="loading && !summary" note="With accepted opportunities." />
      <StatTile label="Opportunities" :value="summary?.opportunity_count" icon="sparkle" :loading="loading && !summary" />
      <StatTile
        v-for="option in FORMATS" :key="option.value"
        :label="option.label" :value="summary?.format_counts[option.value]" :loading="loading && !summary"
        note="By primary format."
      />
    </section>

    <FilterBar :active-count="activeCount" :hidden-active="hiddenActive" @reset="reset">
      <div class="relative min-w-[14rem] flex-1">
        <AppIcon name="search" :size="16" class="pointer-events-none absolute left-3 top-2.5 text-faint" />
        <label class="sr-only" for="social-search">Search</label>
        <input id="social-search" v-model="searchInput" class="field pl-9" placeholder="Search lecture or trainer" />
      </div>
      <div class="flex flex-wrap items-center gap-2" role="group" aria-label="Filter by format">
        <span class="filter-label">Format</span>
        <button
          v-for="option in SOCIAL_FORMAT_FILTERS" :key="option.value" type="button" class="chip cursor-pointer transition"
          :data-test="`format-filter-${option.value || 'all'}`"
          :class="{ '!bg-brand-600 !text-white': filters.content_format === option.value }"
          :aria-pressed="filters.content_format === option.value"
          @click="filters.content_format = option.value"
        >
          {{ option.label }}<span
            v-if="typeof formatTotal(option.value) === 'number' && !filters.content_format"
            class="ml-1" :class="filters.content_format === option.value ? 'text-white/80' : 'text-brand-500'"
          >{{ formatTotal(option.value) }}</span>
        </button>
      </div>

      <template #more>
        <div class="filter-field">
          <span class="filter-label">From</span>
          <label class="sr-only" for="social-from">From date</label>
          <input id="social-from" v-model="filters.date_from" type="date" class="field !w-auto" />
        </div>
        <div class="filter-field">
          <span class="filter-label">To</span>
          <label class="sr-only" for="social-to">To date</label>
          <input id="social-to" v-model="filters.date_to" type="date" class="field !w-auto" />
        </div>
      </template>
    </FilterBar>

    <LoadingSkeleton v-if="loading && !data" :rows="8" />
    <ErrorState v-else-if="error" :message="error" @retry="load" />

    <SectionPanel v-else-if="data" title="Lectures with opportunities" :count="data.count" flush>
      <EmptyState
        v-if="!data.results.length"
        tone="neutral"
        icon="sparkle"
        title="No lectures match these filters"
        message="Try another format, clear the search, or widen the date range."
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
              <th scope="col" class="text-center">Opportunities</th>
              <th v-for="option in FORMATS" :key="option.value" scope="col" class="text-center">{{ option.label }}</th>
              <th scope="col">Recording</th>
              <th scope="col"><span class="sr-only">Open</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in data.results" :key="row.session_id" data-test="social-row">
              <td class="max-w-[22rem]">
                <RouterLink class="row-link" :to="detailLink(row.session_key)">{{ row.subject || 'Untitled lecture' }}</RouterLink>
              </td>
              <td class="whitespace-nowrap text-body">{{ row.trainer || 'Not recorded' }}</td>
              <td class="whitespace-nowrap text-muted">{{ businessDate(row.date) }}</td>
              <td class="text-center">
                <span
                  class="inline-grid h-7 min-w-7 place-items-center rounded-lg bg-brand-50 px-2 text-sm font-bold tabular-nums text-brand-700"
                  data-test="opportunity-count"
                >{{ row.opportunity_count }}</span>
              </td>
              <td
                v-for="option in FORMATS" :key="option.value"
                class="text-center tabular-nums"
                :class="[
                  row.format_counts[option.value] ? 'text-body' : 'text-faint',
                  filters.content_format === option.value ? 'font-bold text-brand-700' : '',
                ]"
                :data-test="`count-${option.value}`"
              >
                {{ row.format_counts[option.value] ?? 0 }}
              </td>
              <td>
                <button
                  v-if="row.recording_available" type="button" class="btn-quiet"
                  @click="row.recording_url && openSafely(row.recording_url)"
                >
                  <AppIcon name="play" :size="14" /> Watch
                </button>
                <StatusBadge v-else label="Link pending" tone="waiting" />
              </td>
              <td class="whitespace-nowrap text-right">
                <RouterLink class="btn-quiet" :to="detailLink(row.session_key)" :aria-label="`View opportunities in ${row.subject}`">
                  View opportunities <AppIcon name="arrowRight" :size="13" />
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
