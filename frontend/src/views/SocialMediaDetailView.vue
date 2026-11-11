<script setup lang="ts">
/**
 * Every accepted social media opportunity in one lecture.
 *
 * Built for scanning: each opportunity is one compact row-card, and the full
 * quote, reasoning and media outline open only for the ones worth a closer
 * look. The opportunities, their counts and every clip state arrive from the
 * API; the format filter only selects on the saved primary format, so its
 * counts are the same ones the lecture list shows. No approval, scheduling or
 * publishing lives here. While a requested clip is processing, its state is
 * re-read from the API (never re-requested) for a bounded time.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppIcon from '../components/AppIcon.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import LoadingSkeleton from '../components/LoadingSkeleton.vue'
import PageHeader from '../components/PageHeader.vue'
import SocialOpportunityCard from '../components/SocialOpportunityCard.vue'
import SocialReanalysis from '../components/SocialReanalysis.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { getSocialClips, getSocialLecture } from '../services/social'
import type { SocialClip, SocialFormat, SocialLectureDetail } from '../types/social'
import { businessDate, cairoDateTime } from '../utils/datetime'
import { errorMessage, openSafely } from '../utils/format'
import { NOT_APPROVED, SOCIAL_FORMAT_FILTERS } from '../utils/social'

const route = useRoute()
const router = useRouter()
const lecture = ref<SocialLectureDetail | null>(null)
const loading = ref(true)
const error = ref('')
const expanded = ref(new Set<number>())

/** Clip status checks: every 20 s while a clip is processing, for at most 15 minutes. */
const POLL_MS = 20_000
const MAX_POLLS = 45
let pollsLeft = MAX_POLLS
let pollTimer: ReturnType<typeof setTimeout> | undefined

const sessionKey = computed(() => String(route.params.sessionKey))

async function load() {
  loading.value = true
  error.value = ''
  try {
    lecture.value = await getSocialLecture(sessionKey.value)
    pollsLeft = MAX_POLLS
    schedulePoll()
  } catch (caught) {
    error.value = errorMessage(caught, 'These social media opportunities could not be loaded.')
  } finally {
    loading.value = false
  }
}
onMounted(load)
onBeforeUnmount(() => clearTimeout(pollTimer))

/** After a reanalysis completes: re-read the opportunities in place, keeping the page. */
async function refreshOpportunities() {
  try {
    lecture.value = await getSocialLecture(sessionKey.value)
    expanded.value = new Set()
    pollsLeft = MAX_POLLS
    schedulePoll()
  } catch (caught) {
    error.value = errorMessage(caught, 'The refreshed opportunities could not be loaded.')
  }
}

const format = ref(typeof route.query.format === 'string' ? route.query.format : '')
watch(format, (value) => {
  void router.replace({ query: { ...route.query, format: value || undefined } })
})

function formatCount(value: string) {
  if (!lecture.value) return 0
  return value ? lecture.value.format_counts[value as SocialFormat] ?? 0 : lecture.value.opportunity_count
}

const visible = computed(() => (lecture.value?.opportunities ?? [])
  .filter((item) => !format.value || item.primary_format === format.value))

// --- details ---------------------------------------------------------------
function toggle(id: number) {
  const next = new Set(expanded.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expanded.value = next
}
const allOpen = computed(() => visible.value.length > 0 && visible.value.every((item) => expanded.value.has(item.opportunity_id)))
function setAll(open: boolean) {
  expanded.value = open ? new Set(visible.value.map((item) => item.opportunity_id)) : new Set()
}

// --- clips -----------------------------------------------------------------
const processing = computed(() => (lecture.value?.opportunities ?? []).some((item) => item.clip?.state === 'requested'))

function setClip(opportunityId: number, clip: SocialClip) {
  const item = lecture.value?.opportunities.find((entry) => entry.opportunity_id === opportunityId)
  if (item) item.clip = clip
  pollsLeft = MAX_POLLS
  schedulePoll()
}

function schedulePoll() {
  clearTimeout(pollTimer)
  if (processing.value && pollsLeft > 0) pollTimer = setTimeout(poll, POLL_MS)
}

async function poll() {
  pollsLeft -= 1
  try {
    const clips = await getSocialClips(sessionKey.value)
    for (const item of lecture.value?.opportunities ?? []) {
      const clip = clips[String(item.opportunity_id)]
      if (clip) item.clip = clip
    }
  } catch {
    // A missed check is retried on the next tick; the shown state stays the last real one.
  }
  schedulePoll()
}
</script>

<template>
  <div class="page stack">
    <RouterLink class="btn-quiet" :to="{ name: 'social-media', query: format ? { format } : {} }">
      <AppIcon name="chevronLeft" :size="14" /> Social Media
    </RouterLink>

    <LoadingSkeleton v-if="loading" variant="cards" :rows="3" />
    <ErrorState v-else-if="error" :message="error" @retry="load" />

    <template v-else-if="lecture">
      <PageHeader kicker="Social Media" :title="lecture.subject || 'Untitled lecture'">
        <template #meta>
          <div class="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-sm text-muted">
            <span class="inline-flex items-center gap-1.5">
              <AppIcon name="users" :size="15" />{{ lecture.trainer || 'Trainer not recorded' }}
            </span>
            <span class="inline-flex items-center gap-1.5">
              <AppIcon name="calendar" :size="15" />{{ businessDate(lecture.date) }}
            </span>
            <span v-if="lecture.last_analyzed_at" class="inline-flex items-center gap-1.5">
              <AppIcon name="sparkle" :size="15" />Analysed {{ cairoDateTime(lecture.last_analyzed_at) }}
            </span>
          </div>
          <div class="mt-3 flex flex-wrap gap-2">
            <StatusBadge
              :label="`${lecture.opportunity_count} opportunit${lecture.opportunity_count === 1 ? 'y' : 'ies'}`"
              tone="brand" data-test="total"
            />
            <StatusBadge
              :label="lecture.recording_available ? 'Recording available' : 'Recording link pending'"
              :tone="lecture.recording_available ? 'ok' : 'waiting'"
            />
          </div>
        </template>
        <template #actions>
          <button
            type="button" class="btn-primary" :disabled="!lecture.recording_available"
            @click="lecture.recording_url && openSafely(lecture.recording_url)"
          >
            <AppIcon name="play" :size="16" />
            {{ lecture.recording_available ? 'Open original recording' : 'Recording link pending' }}
          </button>
        </template>
      </PageHeader>

      <p class="notice-warn" role="note" data-test="not-approved-notice">
        <AppIcon name="info" :size="16" class="mt-0.5" />
        <span><strong>{{ NOT_APPROVED }}.</strong> Every opportunity on this page is an AI suggestion. Check it against the recording before any use.</span>
      </p>

      <SocialReanalysis
        :session-key="lecture.session_key" :opportunity-count="lecture.opportunity_count"
        @completed="refreshOpportunities"
      />

      <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div class="flex flex-wrap items-center gap-2" role="group" aria-label="Filter by format">
          <span class="filter-label">Format</span>
          <button
            v-for="option in SOCIAL_FORMAT_FILTERS" :key="option.value" type="button" class="chip cursor-pointer transition"
            :data-test="`format-filter-${option.value || 'all'}`"
            :class="{ '!bg-brand-600 !text-white': format === option.value }" :aria-pressed="format === option.value"
            @click="format = option.value"
          >
            {{ option.label }}<span class="ml-1" :class="format === option.value ? 'text-white/80' : 'text-brand-500'">{{ formatCount(option.value) }}</span>
          </button>
        </div>
        <button
          v-if="visible.length" type="button" class="btn-quiet btn-sm self-start sm:self-auto" data-test="toggle-all"
          @click="setAll(!allOpen)"
        >
          {{ allOpen ? 'Collapse all' : 'Expand all' }}
        </button>
      </div>

      <EmptyState
        v-if="!visible.length"
        class="surface" tone="neutral" icon="sparkle"
        title="No opportunities in this format"
        message="This lecture's opportunities are in other formats."
      >
        <template #action><button type="button" class="btn-secondary" @click="format = ''">Show all formats</button></template>
      </EmptyState>

      <div v-else class="space-y-2.5">
        <SocialOpportunityCard
          v-for="item in visible" :key="item.opportunity_id" :opportunity="item"
          :open="expanded.has(item.opportunity_id)" :session-key="lecture.session_key"
          @toggle="toggle(item.opportunity_id)" @clip="setClip(item.opportunity_id, $event)"
        />
      </div>
    </template>
  </div>
</template>
