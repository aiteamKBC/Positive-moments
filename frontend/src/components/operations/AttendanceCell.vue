<script setup lang="ts">
/**
 * Attendance, stated honestly.
 *
 * THE BUG THIS COMPONENT EXISTS TO PREVENT
 * ----------------------------------------
 * The legacy compatibility layer writes a numeric zero when the attendance
 * source has not answered yet. Rendering that as "0 learners attended" states
 * something the platform does not know and that is very likely false - it
 * reads as "nobody turned up" when the truth is "nobody has told us yet".
 *
 * So the authoritative flag decides what is shown, and a count is displayed
 * ONLY when the source is authoritative. When it is not, the headline is
 * "Waiting for attendance source" and the platform's own coverage status is
 * shown underneath, because an operator chasing a stuck lecture needs the
 * platform's word for why rather than a paraphrase of it.
 *
 * Waiting is amber. It is not a failure.
 */
import { computed } from 'vue'
import { ATTENDANCE_COVERAGE, humanise } from '../../utils/labels'

const props = defineProps<{
  authoritative: boolean
  coverageStatus: string | null
  attendedCount?: number | null
  compact?: boolean
}>()

const label = computed(() => {
  if (!props.authoritative) return 'Waiting for attendance source'
  return typeof props.attendedCount === 'number'
    ? `${props.attendedCount} attended`
    : 'Attendance resolved'
})

const detail = computed(() => {
  const status = props.coverageStatus
  if (!status) return props.authoritative ? null : 'No attendance record has arrived'
  return ATTENDANCE_COVERAGE[status] ?? humanise(status)
})
</script>

<template>
  <div class="flex flex-col gap-0.5">
    <span :class="authoritative ? 'badge-ok w-fit' : 'badge-waiting w-fit'">{{ label }}</span>
    <span v-if="detail && !compact" class="text-xs leading-snug text-muted" :title="coverageStatus ?? undefined">
      {{ detail }}
    </span>
  </div>
</template>
