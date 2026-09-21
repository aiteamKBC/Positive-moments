<script setup lang="ts">
/**
 * Run history: the operational audit trail.
 *
 * Every row is a recorded fact about something the platform did. The redesign
 * here is not cosmetic - the previous table led with a truncated UUID and a
 * raw ISO timestamp, which are the two things an operator scanning history
 * never needs first.
 */
import { onMounted, ref } from 'vue'

import AppIcon from '../components/AppIcon.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import LoadingSkeleton from '../components/LoadingSkeleton.vue'
import PageHeader from '../components/PageHeader.vue'
import RunsTable from '../components/operations/RunsTable.vue'
import SectionPanel from '../components/SectionPanel.vue'
import { getRun, getRuns } from '../services/operations'
import { humanise } from '../utils/labels'
import type { PipelineRun } from '../types/operations'

const runs = ref<PipelineRun[]>([])
const items = ref<Record<string, unknown>[]>([])
const expanded = ref<string | null>(null)
const loading = ref(true)
const itemsLoading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    runs.value = (await getRuns(50)).runs
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'Run history could not be loaded.'
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function toggle(runId: string) {
  if (expanded.value === runId) { expanded.value = null; return }
  expanded.value = runId
  items.value = []
  itemsLoading.value = true
  try {
    const detail = await getRun(runId)
    items.value = Array.isArray(detail.items) ? detail.items as Record<string, unknown>[] : []
  } catch {
    items.value = []
  } finally {
    itemsLoading.value = false
  }
}
</script>

<template>
  <div class="page stack">
    <RouterLink class="btn-quiet" :to="{ name: 'operations' }">
      <AppIcon name="chevronLeft" :size="14" /> Operations
    </RouterLink>

    <PageHeader
      kicker="Operations"
      title="Run history"
      lede="Every scheduler cycle and operator action the platform has recorded. Opening this page performs read-only requests and starts nothing."
    />

    <LoadingSkeleton v-if="loading" :rows="8" />
    <ErrorState v-else-if="error" :message="error" @retry="load" />

    <SectionPanel v-else title="Recorded runs" :count="runs.length" flush>
      <EmptyState
        v-if="!runs.length"
        tone="neutral"
        icon="clock"
        title="No pipeline runs recorded"
        message="Runs appear here once the scheduler or an operator action has executed."
      />
      <RunsTable v-else :runs="runs" :expanded-id="expanded" @toggle="toggle">
        <template #detail>
          <div class="mt-3 border-t border-line pt-3">
            <p class="eyebrow">Lectures touched by this run</p>
            <p v-if="itemsLoading" class="mt-2 text-xs text-muted">Reading the run detail…</p>
            <p v-else-if="!items.length" class="mt-2 text-xs text-muted">
              This run recorded no per-lecture items.
            </p>
            <ul v-else class="mt-2 space-y-1.5">
              <li v-for="(item, index) in items" :key="index" class="flex flex-wrap items-baseline gap-x-2 text-xs">
                <span class="font-semibold text-ink">{{ item.subject ?? item.lecture_id }}</span>
                <span class="text-muted">{{ humanise(String(item.action ?? '')) }}</span>
                <span class="text-faint">→ {{ humanise(String(item.status ?? '')) }}</span>
                <span v-if="item.error_message_safe" class="text-rose-700">{{ item.error_message_safe }}</span>
              </li>
            </ul>
          </div>
        </template>
      </RunsTable>
    </SectionPanel>
  </div>
</template>
