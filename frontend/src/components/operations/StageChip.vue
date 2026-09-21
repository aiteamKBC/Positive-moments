<script setup lang="ts">
/**
 * One stage state, coloured by what it MEANS rather than by severity.
 *
 * The tone map lives in `utils/labels` so that this chip, the day table's
 * status column and the lecture detail cannot drift into three different
 * opinions about what amber means. The rule that matters is recorded there:
 * WAITING is amber, not red.
 */
import { computed } from 'vue'
import { TONE_CLASS, stageState } from '../../utils/labels'
import type { StageState } from '../../types/operations'

const props = defineProps<{ state: StageState; compact?: boolean }>()
const meta = computed(() => stageState(props.state))
</script>

<template>
  <span :class="TONE_CLASS[meta.tone]" :title="state">{{ compact ? meta.short : meta.label }}</span>
</template>
