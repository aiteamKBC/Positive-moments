<script setup lang="ts">
/**
 * Pipeline runs, for people.
 *
 * The previous table led with `2a3e46f2`, `MANUAL`, `COMPLETED` and
 * `2026-09-20T10:34:35.571541+00:00`. All four are true and none of them is
 * what an operator is looking for, which is: what ran, did it work, when, and
 * what did it touch.
 *
 * So the run's identity is its type and its moment; the UUID moves into the
 * expandable technical row, where it stays copyable for support.
 */
import AppIcon from '../AppIcon.vue'
import StatusBadge from '../StatusBadge.vue'
import { runStatus, runTypeLabel } from '../../utils/labels'
import { businessDate, cairoDateTime, elapsed, sinceNow } from '../../utils/datetime'
import type { PipelineRun } from '../../types/operations'

withDefaults(defineProps<{ runs: PipelineRun[]; compact?: boolean; expandedId?: string | null }>(), {
  compact: false,
  expandedId: null,
})
defineEmits<{ toggle: [runId: string] }>()

function num(run: PipelineRun, key: string) {
  const value = run[key]
  return typeof value === 'number' ? value : 0
}

function text(run: PipelineRun, key: string) {
  const value = run[key]
  return typeof value === 'string' ? value : null
}

/** "1 lecture · 1 waiting" - the outcome, not a row of unlabelled integers. */
function outcome(run: PipelineRun) {
  const seen = num(run, 'lectures_seen')
  const parts = [`${seen} lecture${seen === 1 ? '' : 's'}`]
  for (const [key, word] of [['completed_count', 'complete'], ['waiting_count', 'waiting'],
    ['review_count', 'review'], ['failed_count', 'failed']] as const) {
    if (num(run, key) > 0) parts.push(`${num(run, key)} ${word}`)
  }
  return parts.join(' · ')
}

function dayWindow(run: PipelineRun) {
  const target = text(run, 'target_date')
  if (target) return businessDate(target)
  const from = text(run, 'window_start')
  const to = text(run, 'window_end')
  if (!from) return '—'
  return from === to ? businessDate(from) : `${businessDate(from)} – ${businessDate(to)}`
}
</script>

<template>
  <div class="table-wrap">
    <table class="data-table" :class="compact ? '' : 'min-w-[900px]'">
      <thead>
        <tr>
          <th scope="col">Run</th>
          <th scope="col">Status</th>
          <th scope="col">Business day</th>
          <th scope="col">Outcome</th>
          <th v-if="!compact" scope="col">External work</th>
          <th scope="col" class="text-right">Started</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="run in runs" :key="String(run.run_id)">
          <tr>
            <td>
              <button
                type="button"
                class="flex items-center gap-1.5 text-left font-semibold text-ink transition hover:text-brand-700"
                :aria-expanded="expandedId === String(run.run_id)"
                @click="$emit('toggle', String(run.run_id))"
              >
                <AppIcon
                  name="chevronRight" :size="14"
                  class="text-faint transition"
                  :class="expandedId === String(run.run_id) ? 'rotate-90' : ''"
                />
                {{ runTypeLabel(text(run, 'run_type')) }}
              </button>
            </td>
            <td>
              <StatusBadge
                :label="runStatus(text(run, 'status')).label"
                :tone="runStatus(text(run, 'status')).tone"
                :title="text(run, 'status')"
              />
            </td>
            <td class="whitespace-nowrap text-muted">{{ dayWindow(run) }}</td>
            <td class="text-body">{{ outcome(run) }}</td>
            <td v-if="!compact" class="whitespace-nowrap text-muted">
              <span
                v-if="num(run, 'graph_calls') + num(run, 'provider_calls') + num(run, 'legacy_rows_written') === 0"
                class="badge-quiet"
              >No external calls</span>
              <span v-else class="text-xs">
                {{ num(run, 'graph_calls') }} Graph ·
                {{ num(run, 'provider_calls') }} model ·
                {{ num(run, 'legacy_rows_written') }} legacy write{{ num(run, 'legacy_rows_written') === 1 ? '' : 's' }}
              </span>
            </td>
            <td class="whitespace-nowrap text-right">
              <span class="block text-body">{{ cairoDateTime(text(run, 'started_at')) }}</span>
              <span class="block text-2xs text-faint">{{ sinceNow(text(run, 'started_at')) }}</span>
            </td>
          </tr>

          <tr v-if="expandedId === String(run.run_id)" class="bg-[#fbfafc] hover:bg-[#fbfafc]">
            <td :colspan="compact ? 5 : 6" class="px-4 py-3">
              <dl class="grid gap-x-8 gap-y-2 text-xs sm:grid-cols-2 lg:grid-cols-3">
                <div>
                  <dt class="eyebrow">Run identifier</dt>
                  <dd class="mt-0.5 break-all font-mono text-xs text-body">{{ run.run_id }}</dd>
                </div>
                <div>
                  <dt class="eyebrow">Finished</dt>
                  <dd class="mt-0.5 text-body">
                    {{ cairoDateTime(text(run, 'finished_at')) }}
                    <span v-if="elapsed(text(run, 'started_at'), text(run, 'finished_at'))" class="text-faint">
                      ({{ elapsed(text(run, 'started_at'), text(run, 'finished_at')) }})
                    </span>
                  </dd>
                </div>
                <div>
                  <dt class="eyebrow">Orchestration version</dt>
                  <dd class="mt-0.5 font-mono text-xs text-body">{{ text(run, 'orchestration_version') ?? '—' }}</dd>
                </div>
                <div>
                  <dt class="eyebrow">Legacy QA precheck</dt>
                  <dd class="mt-0.5 text-body">{{ text(run, 'legacy_qa_precheck_status') ?? '—' }}</dd>
                </div>
                <div>
                  <dt class="eyebrow">Lectures seen / skipped</dt>
                  <dd class="mt-0.5 text-body tabular-nums">{{ num(run, 'lectures_seen') }} / {{ num(run, 'skipped_count') }}</dd>
                </div>
                <div>
                  <dt class="eyebrow">Complete / waiting / review / failed</dt>
                  <dd class="mt-0.5 text-body tabular-nums">
                    {{ num(run, 'completed_count') }} / {{ num(run, 'waiting_count') }} /
                    {{ num(run, 'review_count') }} / {{ num(run, 'failed_count') }}
                  </dd>
                </div>
              </dl>
              <slot name="detail" :run="run" />
            </td>
          </tr>
        </template>
      </tbody>
    </table>
  </div>
  <p v-if="!runs.length" class="px-4 py-6 text-center text-sm text-muted">No pipeline runs recorded.</p>
</template>
