<script setup lang="ts">
/**
 * Reanalyse Lecture, and the status of this lecture's latest analysis run.
 *
 * Requesting is one POST: Django records a run and sends {session_id, mode}
 * to n8n. Everything after that is read-only polling of the run record -
 * polling never sends another request. n8n's acknowledgement is not progress:
 * the run shows Running, Completed or Failed only once the stored run says so,
 * and only an observed Completed tells the page to reload its opportunities.
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import AppIcon from './AppIcon.vue'
import StatusBadge from './StatusBadge.vue'
import { getAnalysisRuns, requestReanalysis } from '../services/social'
import type { AnalysisRun } from '../types/social'
import { cairoDateTime } from '../utils/datetime'
import type { Tone } from '../utils/labels'
import { apiError } from '../utils/social'

const props = defineProps<{ sessionKey: string; opportunityCount: number }>()
const emit = defineEmits<{ completed: [run: AnalysisRun] }>()

/** Status checks: every 15 s while a run is active and not stale, for at most 75 minutes. */
const POLL_MS = 15_000
const MAX_POLLS = 300

const latest = ref<AnalysisRun | null>(null)
const loading = ref(true)
const loadError = ref('')
const confirming = ref(false)
const requesting = ref(false)
const feedback = ref<{ tone: 'ok' | 'error' | 'warn'; text: string } | null>(null)
let pollTimer: ReturnType<typeof setTimeout> | undefined
let pollsLeft = MAX_POLLS

const active = computed(() => latest.value !== null
  && (latest.value.status === 'queued' || latest.value.status === 'running') && !latest.value.stale)
const canRequest = computed(() => !loading.value && !requesting.value && !active.value)

const FAILURES: Record<string, string> = {
  dispatch_failed: 'The analysis service did not accept the request.',
  timed_out: 'No result was received within the time allowed.',
  not_dispatched: 'The request was never sent to the analysis service.',
}

const view = computed<{ label: string; tone: Tone; text: string } | null>(() => {
  const run = latest.value
  if (!run) return null
  if (run.stale) {
    return {
      label: 'No result yet', tone: 'review',
      text: `Requested ${cairoDateTime(run.created_at)}. No result has been reported for over an hour; you can request it again.`,
    }
  }
  switch (run.status) {
    case 'queued':
      return {
        label: 'Queued', tone: 'waiting',
        text: run.dispatched_at
          ? `Sent to the analysis service ${cairoDateTime(run.dispatched_at)}. Waiting for it to start.`
          : `Requested ${cairoDateTime(run.created_at)}. Waiting to be sent.`,
      }
    case 'running':
      return { label: 'Running', tone: 'info', text: `Analysis started ${cairoDateTime(run.started_at ?? run.created_at)}.` }
    case 'completed': {
      const found = run.opportunities_verified
      return {
        label: 'Completed', tone: 'ok',
        text: `Finished ${cairoDateTime(run.finished_at)}${typeof found === 'number' ? ` · ${found} opportunit${found === 1 ? 'y' : 'ies'} saved` : ''}.`,
      }
    }
    default:
      return {
        label: 'Failed', tone: 'error',
        text: `${(run.error_code && FAILURES[run.error_code]) || run.error_message || 'The analysis did not complete.'} `
          + `${cairoDateTime(run.finished_at ?? run.created_at)}.`,
      }
  }
})

function schedulePoll() {
  clearTimeout(pollTimer)
  if (active.value && pollsLeft > 0) pollTimer = setTimeout(poll, POLL_MS)
}

async function refresh() {
  const previous = latest.value
  const runs = await getAnalysisRuns(props.sessionKey)
  latest.value = runs[0] ?? null
  const now = latest.value
  // Reload the opportunities only on a completion this page saw happen.
  if (previous && now && previous.run_id === now.run_id
      && previous.status !== 'completed' && now.status === 'completed') {
    feedback.value = { tone: 'ok', text: 'Reanalysis completed. The opportunities below have been refreshed.' }
    emit('completed', now)
  }
}

async function poll() {
  pollsLeft -= 1
  try {
    await refresh()
  } catch {
    // A missed check is retried on the next tick; the shown state stays the last real one.
  }
  schedulePoll()
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    await refresh()
    pollsLeft = MAX_POLLS
    schedulePoll()
  } catch (caught) {
    loadError.value = apiError(caught, 'The analysis status could not be loaded.').detail
  } finally {
    loading.value = false
  }
}
onMounted(load)
onBeforeUnmount(() => clearTimeout(pollTimer))

async function confirmRequest() {
  requesting.value = true
  feedback.value = null
  try {
    latest.value = await requestReanalysis(props.sessionKey)
    confirming.value = false
    feedback.value = {
      tone: 'ok',
      text: 'Reanalysis requested. It runs in the background; this panel updates when the analysis service reports back.',
    }
    pollsLeft = MAX_POLLS
    schedulePoll()
  } catch (caught) {
    const error = apiError(caught, 'Reanalysis could not be requested. Try again.')
    confirming.value = false
    if (error.code === 'analysis_in_progress') {
      feedback.value = { tone: 'warn', text: 'This lecture is already being analysed.' }
      await load()
    } else {
      feedback.value = { tone: 'error', text: error.detail }
      await refresh().catch(() => undefined)
    }
  } finally {
    requesting.value = false
  }
}
</script>

<template>
  <section class="surface" aria-labelledby="reanalysis-title" data-test="reanalysis">
    <div class="panel-head">
      <div class="min-w-0">
        <h2 id="reanalysis-title" class="text-base font-bold text-ink">AI analysis</h2>
        <p class="mt-0.5 text-xs text-muted">Analyse this lecture again with the current AI prompts.</p>
      </div>
      <button
        type="button" class="btn-secondary" :disabled="!canRequest" data-test="reanalyse"
        @click="confirming = true; feedback = null"
      >
        <AppIcon name="refresh" :size="16" />
        {{ active ? 'Reanalysis in progress' : 'Reanalyse lecture' }}
      </button>
    </div>

    <div class="panel-body space-y-3">
      <p v-if="loading" class="text-sm text-muted">Reading the analysis status…</p>
      <div v-else-if="loadError" class="notice-error" role="alert">
        <AppIcon name="alert" :size="16" class="mt-0.5" />
        <span class="flex-1">{{ loadError }}</span>
        <button type="button" class="btn-quiet" @click="load">Retry</button>
      </div>
      <div v-else-if="view" class="flex flex-wrap items-center gap-x-3 gap-y-1.5" data-test="run-status">
        <StatusBadge :label="view.label" :tone="view.tone" data-test="run-badge" />
        <span class="text-sm text-body">{{ view.text }}</span>
        <span v-if="latest?.requested_by" class="text-xs text-muted">Requested by {{ latest.requested_by }}</span>
      </div>
      <p v-else class="text-sm text-muted" data-test="no-runs">No reanalysis has been requested for this lecture.</p>

      <div
        v-if="feedback" role="status" data-test="reanalysis-feedback"
        :class="feedback.tone === 'ok' ? 'notice' : feedback.tone === 'warn' ? 'notice-warn' : 'notice-error'"
      >
        <AppIcon :name="feedback.tone === 'ok' ? 'checkCircle' : 'alert'" :size="16" class="mt-0.5" />
        <span>{{ feedback.text }}</span>
      </div>
    </div>

    <div
      v-if="confirming" class="fixed inset-0 z-50 grid place-items-center bg-brand-950/60 p-4 backdrop-blur-sm"
      role="presentation" @click.self="!requesting && (confirming = false)"
    >
      <section class="surface w-full max-w-lg" role="dialog" aria-modal="true" aria-labelledby="confirm-title" data-test="confirm-reanalysis">
        <div class="panel-head">
          <div>
            <p class="kicker">Social Media</p>
            <h2 id="confirm-title" class="mt-1 text-lg font-bold text-ink">Reanalyse this lecture?</h2>
          </div>
        </div>
        <div class="panel-body space-y-3 text-sm leading-6 text-body">
          <p>
            The AI will analyse this lecture again using the current Social Discovery and Social Verification prompts.
            It runs in the background and can take several minutes.
          </p>
          <p class="notice-warn">
            <AppIcon name="alert" :size="16" class="mt-0.5" />
            <span>
              <strong>A successful reanalysis replaces this lecture's {{ opportunityCount }} current Social Media
                opportunit{{ opportunityCount === 1 ? 'y' : 'ies' }}.</strong>
              They cannot be brought back from this page.
            </span>
          </p>
        </div>
        <div class="flex justify-end gap-2 border-t border-line px-5 py-3">
          <button type="button" class="btn-secondary" :disabled="requesting" @click="confirming = false">Cancel</button>
          <button type="button" class="btn-primary" :disabled="requesting" data-test="confirm" @click="confirmRequest">
            {{ requesting ? 'Requesting…' : 'Reanalyse lecture' }}
          </button>
        </div>
      </section>
    </div>
  </section>
</template>
