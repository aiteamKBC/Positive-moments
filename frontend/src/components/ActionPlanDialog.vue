<script setup lang="ts">
/**
 * The confirmation step for a guarded action.
 *
 * The plan shown here is the one the SERVER generated from the lecture's
 * current persisted state; this dialog formats it and adds nothing. Confirming
 * posts back the action the plan named, so a plan that changed while the tab
 * sat open is refused by the platform rather than executed from a screenshot.
 *
 * The cost block is the point of the whole dialog: before anything runs, the
 * operator is told in plain words whether this will call Microsoft Graph, buy
 * a model generation, write a legacy production row, or touch other lectures.
 */
import { computed } from 'vue'
import AppIcon from './AppIcon.vue'
import { actionLabel, humanise, stageLabel } from '../utils/labels'

const props = defineProps<{
  open: boolean
  title: string
  plan: Record<string, unknown> | null
  busy?: boolean
}>()
const emit = defineEmits<{ close: []; confirm: [] }>()

const COST_LABELS: Record<string, string> = {
  calls_microsoft_graph: 'Calls Microsoft Graph',
  buys_model_generation: 'Buys a model generation',
  writes_legacy_production_row: 'Writes a legacy production row',
  affects_other_lectures_that_day: 'Affects other lectures that day',
  reruns_qa: 'Re-runs the quality evaluation',
}

const cost = computed(() => {
  const value = props.plan?.cost
  if (!value || typeof value !== 'object') return []
  return Object.entries(value as Record<string, unknown>)
    .map(([key, flag]) => ({ key, label: COST_LABELS[key] ?? humanise(key), yes: Boolean(flag) }))
})

const preserved = computed(() => {
  const value = props.plan?.stages_preserved
  return Array.isArray(value) ? (value as string[]) : []
})

const reason = computed(() => {
  const value = props.plan?.retry_reason ?? props.plan?.reason
  return typeof value === 'string' ? value : null
})

const resumeStage = computed(() => {
  const value = props.plan?.resume_stage
  return typeof value === 'string' ? value : null
})

const nextAction = computed(() => {
  const value = props.plan?.action
  return typeof value === 'string' ? value : null
})
</script>

<template>
  <div
    v-if="open"
    class="fixed inset-0 z-50 grid place-items-center bg-brand-950/60 p-4 backdrop-blur-sm"
    role="presentation"
    @click.self="emit('close')"
  >
    <section
      class="surface max-h-[88vh] w-full max-w-lg overflow-y-auto"
      role="dialog"
      aria-modal="true"
      :aria-label="title"
    >
      <div class="panel-head">
        <div>
          <p class="kicker">Guarded action</p>
          <h2 class="mt-1 text-lg font-bold text-ink">{{ title }}</h2>
        </div>
        <button type="button" class="icon-btn" aria-label="Close" @click="emit('close')">
          <AppIcon name="close" :size="16" />
        </button>
      </div>

      <div class="panel-body">
        <p v-if="!plan" class="text-sm text-muted">Reading the current plan from the server…</p>

        <template v-else>
          <p class="text-sm leading-6 text-muted">
            The server generated this plan from the lecture’s persisted state. Confirming performs only the
            named action — it never reprocesses anything that is already done.
          </p>

          <dl class="mt-4 space-y-3">
            <div v-if="nextAction">
              <dt class="eyebrow">Action</dt>
              <dd class="mt-0.5 text-sm font-semibold text-ink">{{ actionLabel(nextAction) }}</dd>
            </div>
            <div v-if="resumeStage">
              <dt class="eyebrow">Resumes at</dt>
              <dd class="mt-0.5 text-sm text-body">{{ stageLabel(resumeStage) }}</dd>
            </div>
            <div v-if="reason">
              <dt class="eyebrow">Platform reason</dt>
              <dd class="mt-0.5 text-sm text-body">{{ humanise(reason) }}</dd>
            </div>
          </dl>

          <div v-if="cost.length" class="mt-5">
            <p class="eyebrow">What this will cost</p>
            <ul class="mt-2 space-y-1.5">
              <li v-for="item in cost" :key="item.key" class="flex items-center gap-2 text-sm">
                <AppIcon
                  :name="item.yes ? 'alert' : 'check'"
                  :size="15"
                  :class="item.yes ? 'text-amber-600' : 'text-emerald-600'"
                />
                <span :class="item.yes ? 'text-ink' : 'text-muted'">
                  {{ item.label }} — <span class="font-semibold">{{ item.yes ? 'yes' : 'no' }}</span>
                </span>
              </li>
            </ul>
          </div>

          <div v-if="preserved.length" class="mt-5">
            <p class="eyebrow">Kept as it is</p>
            <div class="mt-2 flex flex-wrap gap-1">
              <span v-for="stage in preserved" :key="stage" class="chip">{{ stageLabel(stage) }}</span>
            </div>
          </div>
        </template>
      </div>

      <div class="flex justify-end gap-2 border-t border-line px-5 py-3">
        <button type="button" class="btn-secondary" :disabled="busy" @click="emit('close')">Cancel</button>
        <button type="button" class="btn-primary" :disabled="!plan || busy" @click="emit('confirm')">
          {{ busy ? 'Running…' : `Confirm ${title.toLowerCase()}` }}
        </button>
      </div>
    </section>
  </div>
</template>
