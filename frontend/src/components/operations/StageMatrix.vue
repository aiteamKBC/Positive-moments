<script setup lang="ts">
/**
 * The fifteen validated stages, presented as a pipeline rather than a log.
 *
 * The stage list, its order and every state come from the server untouched -
 * this component renames them for reading and says which one is blocking, and
 * decides nothing. The technical payload each stage carries (fingerprints,
 * versions, reason codes, counts) is preserved verbatim behind a disclosure
 * rather than deleted, because support work needs it and the primary view
 * does not.
 */
import { ref } from 'vue'
import AppIcon from '../AppIcon.vue'
import StageChip from './StageChip.vue'
import { STAGE_NOTES, humanise, stageLabel } from '../../utils/labels'
import { cairoDateTime } from '../../utils/datetime'
import type { Stage, StageName } from '../../types/operations'

defineProps<{
  stageOrder: StageName[]
  stages: Record<StageName, Stage>
  blockingStage: string | null
}>()

const showTechnical = ref(false)
const expanded = ref<string | null>(null)

/** Keys already shown in the human summary, or not worth a row of their own. */
const HIDDEN = new Set(['state', 'action', 'reason'])
const TIME_KEYS = ['timestamp', 'updated_at', 'completed_at', 'evaluated_at', 'rendered_at']

function detailEntries(stage: Stage): [string, string][] {
  return Object.entries(stage)
    .filter(([key, value]) => !HIDDEN.has(key) && value !== null && value !== undefined)
    .map(([key, value]) => [key, typeof value === 'object' ? JSON.stringify(value) : String(value)])
}

function stageTime(stage: Stage) {
  for (const key of TIME_KEYS) {
    const value = stage[key]
    if (typeof value === 'string') return value
  }
  return null
}

/** The left rail dot: settled, working, or still to come. */
function dotClass(stage: Stage, isBlocking: boolean) {
  if (isBlocking) return 'bg-brand-700 ring-4 ring-brand-100'
  switch (stage.state) {
    case 'COMPLETE': return 'bg-emerald-500'
    case 'FAILED': return 'bg-rose-500'
    case 'REVIEW_REQUIRED': return 'bg-orange-500'
    case 'WAITING': return 'bg-amber-400'
    case 'NOT_APPLICABLE': return 'bg-line'
    default: return 'bg-slate-300'
  }
}
</script>

<template>
  <div class="panel">
    <div class="panel-head">
      <div>
        <h2 class="section-title">Processing pipeline</h2>
        <p class="mt-0.5 text-xs text-muted">Fifteen stages, in the order the platform runs them.</p>
      </div>
      <button
        type="button"
        class="btn btn-sm border border-line bg-white text-muted hover:border-brand-300 hover:text-brand-700"
        @click="showTechnical = !showTechnical"
      >
        <AppIcon name="layers" :size="14" />
        {{ showTechnical ? 'Hide' : 'Show' }} technical detail
      </button>
    </div>

    <ol class="divide-y divide-line/70">
      <li
        v-for="(name, index) in stageOrder"
        :key="name"
        class="relative px-4 py-3 transition sm:px-5"
        :class="name === blockingStage ? 'bg-brand-50/60' : ''"
      >
        <div class="flex items-start gap-3">
          <!-- rail -->
          <div class="relative flex w-4 shrink-0 justify-center pt-1.5">
            <span class="h-2.5 w-2.5 rounded-full" :class="dotClass(stages[name], name === blockingStage)" />
            <span
              v-if="index < stageOrder.length - 1"
              class="absolute left-1/2 top-5 h-[calc(100%-0.25rem)] w-px -translate-x-1/2 bg-line"
              aria-hidden="true"
            />
          </div>

          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
              <p class="text-sm font-semibold text-ink">{{ stageLabel(name) }}</p>
              <span
                v-if="name === blockingStage"
                class="rounded bg-brand-700 px-1.5 py-0.5 text-2xs font-bold uppercase tracking-wide text-white"
              >Current step</span>
            </div>
            <p class="mt-0.5 text-xs leading-relaxed text-muted">{{ STAGE_NOTES[name] }}</p>
            <p v-if="stages[name].reason" class="mt-1 text-xs text-body">
              {{ humanise(stages[name].reason) }}
            </p>
            <p v-if="stageTime(stages[name])" class="mt-1 text-2xs text-faint">
              Updated {{ cairoDateTime(stageTime(stages[name])) }}
            </p>

            <div v-if="showTechnical" class="mt-2">
              <button
                type="button"
                class="text-2xs font-semibold uppercase tracking-kicker text-faint transition hover:text-brand-700"
                @click="expanded = expanded === name ? null : name"
              >
                {{ expanded === name ? 'Hide' : 'Show' }} raw stage payload
              </button>
              <dl
                v-if="expanded === name"
                class="mt-2 grid gap-x-6 gap-y-1.5 rounded-lg border border-line bg-[#fbfafc] p-3 sm:grid-cols-2"
              >
                <div v-for="[key, value] in detailEntries(stages[name])" :key="key" class="min-w-0">
                  <dt class="text-2xs font-semibold uppercase tracking-kicker text-faint">{{ key }}</dt>
                  <dd class="break-all font-mono text-xs text-body">{{ value }}</dd>
                </div>
              </dl>
            </div>
          </div>

          <div class="shrink-0 pt-0.5"><StageChip :state="stages[name].state" /></div>
        </div>
      </li>
    </ol>
  </div>
</template>
