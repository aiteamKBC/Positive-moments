<script setup lang="ts">
/**
 * The publisher's OPEN alerts, exactly as the reconciler stored them (bunny_publisher_alerts).
 *
 * Read-only: no acknowledge / resolve buttons - the reconciler resolves an alert itself when its
 * condition is no longer observed. A lecture or job is shown only when the alert names one; every
 * other stored detail is listed as it is. Nothing is inferred.
 */
import { onMounted, ref, watch } from 'vue'

import AppIcon from '../AppIcon.vue'
import EmptyState from '../EmptyState.vue'
import SectionPanel from '../SectionPanel.vue'
import StatusBadge from '../StatusBadge.vue'
import { getPublisherAlerts } from '../../services/publishing'
import type { PublisherAlert } from '../../types/publishing'
import { businessDate, cairoDateTime, sinceNow } from '../../utils/datetime'
import { errorMessage } from '../../utils/format'
import { humanise } from '../../utils/labels'
import { label } from '../../utils/publishing'

const props = defineProps<{ refreshKey: string }>()
const emit = defineEmits<{ close: []; openLecture: [lectureId: string] }>()

const alerts = ref<PublisherAlert[] | null>(null)
const error = ref('')

async function load() {
  try {
    alerts.value = (await getPublisherAlerts()).alerts
    error.value = ''
  } catch (caught) {
    error.value = errorMessage(caught, 'The publisher alerts could not be read.')
  }
}

onMounted(load)
watch(() => props.refreshKey, load)

function detailValue(value: unknown) {
  if (value === null || value === undefined || value === '') return '—'
  if (Array.isArray(value)) return value.join(', ')
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
</script>

<template>
  <SectionPanel
    title="Open publisher alerts"
    :count="alerts?.length ?? null"
    note="As recorded by the publisher's reconciliation. An alert closes by itself when its condition is no longer observed."
    flush
  >
    <template #actions>
      <button type="button" class="btn-quiet btn-sm" @click="emit('close')">
        <AppIcon name="close" :size="14" /> Hide
      </button>
    </template>
    <p v-if="error" class="notice-error m-4">{{ error }}</p>
    <div v-else-if="!alerts" class="p-4"><div class="skeleton h-16 w-full" /></div>
    <EmptyState v-else-if="!alerts.length" compact title="No open alerts" message="The publisher has nothing open right now." />
    <div v-else class="overflow-x-auto">
      <table class="data-table">
        <thead>
          <tr>
            <th scope="col">Severity</th>
            <th scope="col">Alert</th>
            <th scope="col">Lecture / job</th>
            <th scope="col">Details</th>
            <th scope="col" class="text-right">Seen</th>
            <th scope="col">First seen</th>
            <th scope="col">Last seen</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="alert in alerts" :key="alert.alert_id">
            <td><StatusBadge v-bind="label.alert(alert.severity)" /></td>
            <td class="min-w-[14rem]">
              <p class="font-semibold text-ink">{{ humanise(alert.kind) }}</p>
              <p class="text-xs text-muted">{{ alert.summary }}</p>
            </td>
            <td class="min-w-[10rem]">
              <button
                v-if="alert.lecture" type="button" class="row-link text-left text-sm font-semibold"
                @click="emit('openLecture', alert.lecture.lecture_id)"
              >
                {{ alert.lecture.subject ?? 'Lecture' }}
                <span v-if="alert.lecture.session_date" class="block text-2xs font-normal text-muted">{{ businessDate(alert.lecture.session_date) }}</span>
              </button>
              <p v-if="alert.job_id" class="font-mono text-2xs text-muted">job {{ alert.job_id }}</p>
              <span v-if="!alert.lecture && !alert.job_id" class="text-xs text-faint">Not linked to a lecture</span>
            </td>
            <td class="min-w-[12rem]">
              <dl v-if="Object.keys(alert.detail).length" class="space-y-0.5 text-2xs">
                <div v-for="(value, key) in alert.detail" :key="key" class="flex gap-1.5">
                  <dt class="shrink-0 text-faint">{{ humanise(String(key)) }}</dt>
                  <dd class="break-all font-mono text-muted">{{ detailValue(value) }}</dd>
                </div>
              </dl>
              <span v-else class="text-xs text-faint">—</span>
            </td>
            <td class="text-right tabular-nums">{{ alert.occurrences }}×</td>
            <td class="whitespace-nowrap text-xs text-muted">{{ cairoDateTime(alert.first_seen_at) }}</td>
            <td class="whitespace-nowrap text-xs text-muted" :title="cairoDateTime(alert.last_seen_at)">{{ sinceNow(alert.last_seen_at, '—') }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </SectionPanel>
</template>
